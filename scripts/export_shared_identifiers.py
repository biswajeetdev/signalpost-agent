#!/usr/bin/env python3
"""Export the registry-wide share counts the website identity gate needs into a small shipped snapshot.

Only identifiers held by two or more entities are kept (administrator email domains and phones,
namesake legal-name keys); an absent identifier is unshared. Reads a cache built by
scripts/build_official_cache.py and writes data/shared-identifiers.json.gz.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True, help="official.sqlite from scripts/build_official_cache.py")
    parser.add_argument("--output", default=str(ROOT / "data" / "shared-identifiers.json.gz"))
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{args.cache}?mode=ro", uri=True)
    source_url, sha256, retrieved_at, rows = db.execute(
        "SELECT source_url, sha256, retrieved_at, rows FROM snapshots WHERE name = 'shared_identifiers'"
    ).fetchone()
    body = {
        "snapshot": {"source_url": source_url, "sha256": sha256, "retrieved_at": retrieved_at, "rows": rows},
        "note": "Share counts for identifiers held by 2+ registry entities; absent means unshared.",
    }
    for table, key in (("email_domains", "domain"), ("phones", "phone"), ("name_keys", "key")):
        body[table] = dict(db.execute(f"SELECT {key}, entities FROM {table} WHERE entities > 1 ORDER BY {key}"))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(output, "wb", mtime=0) as handle:
        handle.write(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    print(json.dumps({table: len(body[table]) for table in ("email_domains", "phones", "name_keys")} | {"bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
