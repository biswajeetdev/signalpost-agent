from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .evidence import evidence
from .official import BRREG_ROLES, BRREG_SUBUNITS

# (module, table, source class, per-entity URL, snapshot name, note when the snapshot has no row)
CACHED_MODULES = (
    ("roles", "roles", "official_roles_bulk_snapshot", BRREG_ROLES, "roles", "No roles for this entity in the official bulk roles snapshot"),
    ("locations", "locations", "official_subunits_bulk_snapshot", BRREG_SUBUNITS, "locations", "No active registered subunits for this entity in the official bulk snapshot"),
)


class OfficialCache:
    """Read-only access to the declared local cache built by scripts/build_official_cache.py."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Official cache not found: {self.path}")
        self._local = threading.local()
        self.snapshots = {
            name: {"source_url": source_url, "sha256": sha256, "retrieved_at": retrieved_at, "rows": rows}
            for name, source_url, sha256, retrieved_at, rows in self._db().execute(
                "SELECT name, source_url, sha256, retrieved_at, rows FROM snapshots"
            )
        }

    def _db(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._local.connection = connection
        return connection

    def share_count(self, table: str, value: str) -> int:
        key = {"email_domains": "domain", "phones": "phone"}[table]
        row = self._db().execute(f"SELECT entities FROM {table} WHERE {key} = ?", (value,)).fetchone()
        return int(row[0]) if row else 0

    def shared_domains(self) -> "_ShareCounts":
        return _ShareCounts(self, "email_domains")

    def shared_phones(self) -> "_ShareCounts":
        return _ShareCounts(self, "phones")

    def module_records(self, org: str, modules: set[str] | None = None) -> dict[str, dict[str, Any]]:
        records = {}
        for module, table, source_class, url, snapshot_name, empty_note in CACHED_MODULES:
            if modules is not None and module not in modules:
                continue
            snapshot = self.snapshots[snapshot_name]
            provenance = f"Declared bulk snapshot {snapshot['source_url']} (sha256 {snapshot['sha256']})"
            row = self._db().execute(f"SELECT body, content_sha256 FROM {table} WHERE organisation_number = ?", (org,)).fetchone()
            if row is None:
                records[module] = evidence(
                    module, "not_available", source_class, url.format(org=org),
                    note=f"{empty_note}. {provenance}", retrieved_at=snapshot["retrieved_at"], source_row_key=org,
                )
                continue
            records[module] = evidence(
                module, "available", source_class, url.format(org=org),
                value=json.loads(row[0]), note=provenance, retrieved_at=snapshot["retrieved_at"],
                content_sha256=row[1], source_row_key=org,
            )
        return records


class _ShareCounts:
    """Mapping-style `.get` over a share-count table, for candidate and proof gates."""

    def __init__(self, cache: OfficialCache, table: str) -> None:
        self._cache = cache
        self._table = table

    def get(self, value: str, default: int = 0) -> int:
        return self._cache.share_count(self._table, value) or default
