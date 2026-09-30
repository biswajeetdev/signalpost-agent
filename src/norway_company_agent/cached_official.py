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
        key = {"email_domains": "domain", "phones": "phone", "name_keys": "key"}[table]
        row = self._db().execute(f"SELECT entities FROM {table} WHERE {key} = ?", (value,)).fetchone()
        return int(row[0]) if row else 0

    def shared_domains(self) -> "_ShareCounts":
        return _ShareCounts(self, "email_domains")

    def shared_phones(self) -> "_ShareCounts":
        return _ShareCounts(self, "phones")

    def name_keys(self) -> "_ShareCounts":
        return _ShareCounts(self, "name_keys")

    def module_records(self, org: str, modules: set[str] | None = None, fetch: Any = None) -> dict[str, dict[str, Any]]:
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


# Shipped with the code: registry-wide share counts for identifiers held by 2+ entities (administrator
# email domains and phones, namesake legal names), built by scripts/export_shared_identifiers.py from
# the Brønnøysund entity bulk snapshot. Needed by the website identity gate on a clean clone.
SHARED_SNAPSHOT = Path(__file__).resolve().parents[2] / "data" / "shared-identifiers.json.gz"


class _SharedOnly:
    """Counts for shared identifiers only. An absent key is unshared: `absent` is 0 for domains and
    phones; 1 for name keys, because the company's own legal name is always in the registry once."""

    def __init__(self, counts: dict[str, int], absent: int) -> None:
        self._counts = counts
        self._absent = absent

    def get(self, value: str, default: int = 0) -> int:
        count = self._counts.get(value)
        if count is not None:
            return count
        return self._absent if value else default


class LiveOfficialCache:
    """Fallback when no built cache is supplied: roles and workplaces come from the live per-entity
    Brønnøysund endpoints (charged to the run budget), share counts from the shipped snapshot."""

    def __init__(self, shared_path: str | Path = SHARED_SNAPSHOT) -> None:
        import gzip

        with gzip.open(shared_path, "rt", encoding="utf-8") as handle:
            body = json.load(handle)
        self.snapshots = {
            "shared_identifiers": {k: body["snapshot"][k] for k in ("source_url", "sha256", "retrieved_at", "rows")},
            "roles": {"source_url": BRREG_ROLES, "sha256": "live", "retrieved_at": None, "rows": None},
            "locations": {"source_url": BRREG_SUBUNITS, "sha256": "live", "retrieved_at": None, "rows": None},
        }
        self._domains = _SharedOnly(body["email_domains"], 0)
        self._phones = _SharedOnly(body["phones"], 0)
        self._names = _SharedOnly(body["name_keys"], 1)

    def shared_domains(self) -> _SharedOnly:
        return self._domains

    def shared_phones(self) -> _SharedOnly:
        return self._phones

    def name_keys(self) -> _SharedOnly:
        return self._names

    def module_records(self, org: str, modules: set[str] | None = None, fetch: Any = None) -> dict[str, dict[str, Any]]:
        from .official import fetch_official_modules

        wanted = {"roles", "locations"} if modules is None else {"roles", "locations"} & set(modules)
        if not wanted:
            return {}
        records, _ = fetch_official_modules(org, wanted, **({"fetcher": fetch} if fetch else {}))
        return records


def open_official_cache(path: str | Path | None) -> OfficialCache | LiveOfficialCache:
    """Accept whatever the evaluator supplies: a built cache file, a directory holding one, a missing or
    empty path. Anything that is not a readable built cache falls back to live official endpoints."""
    candidates: list[Path] = []
    if path:
        given = Path(path)
        candidates += [given / "official.sqlite", given / "cache" / "official.sqlite"] if given.is_dir() else [given]
    candidates.append(Path(__file__).resolve().parents[2] / "cache" / "official.sqlite")
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            try:
                cache = OfficialCache(candidate)
                if {"roles", "locations", "shared_identifiers"} <= set(cache.snapshots):
                    return cache
            except sqlite3.Error:
                continue
    return LiveOfficialCache()
