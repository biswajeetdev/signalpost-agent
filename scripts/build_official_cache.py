#!/usr/bin/env python3
"""Build the declared local official cache from Brønnøysund bulk snapshots.

Inputs (public bulk downloads, no API key):
  roller/totalbestand            -> roles per organisation (same shape as /enheter/{org}/roller)
  underenheter/lastned/csv       -> registered workplaces per parent organisation
  enheter/lastned/csv            -> company-owned email-domain share counts

Only organisations in the eligible universe are stored. Birth dates are never stored.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.candidates import email_domain_counts  # noqa: E402
from norway_company_agent.evidence import utc_now  # noqa: E402
from norway_company_agent.official import normalize_roles  # noqa: E402
from norway_company_agent.proof import phone_share_counts  # noqa: E402

ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/roller/totalbestand"
SUBUNITS_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"
ENTITIES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def universe_numbers(path: Path) -> set[str]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return {json.loads(line)["organisation_number"] for line in handle if line.strip()}


def iter_json_array(path: Path, chunk_size: int = 1 << 20):
    """Stream objects from a large gzipped JSON array without loading it into memory."""
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        while True:
            chunk = handle.read(chunk_size)
            buffer += chunk
            if not started:
                stripped = buffer.lstrip()
                if not stripped:
                    if not chunk:
                        return
                    continue
                if stripped[0] != "[":
                    raise ValueError("Expected a JSON array")
                buffer = stripped[1:]
                started = True
            while True:
                buffer = buffer.lstrip().lstrip(",").lstrip()
                if not buffer or buffer[0] == "]":
                    break
                try:
                    item, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                yield item
                buffer = buffer[end:]
            if not chunk:
                return


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, source_url TEXT, sha256 TEXT, built_at TEXT, rows INTEGER);
        CREATE TABLE IF NOT EXISTS roles (organisation_number TEXT PRIMARY KEY, body TEXT, content_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS locations (organisation_number TEXT PRIMARY KEY, body TEXT, content_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS email_domains (domain TEXT PRIMARY KEY, entities INTEGER);
        CREATE TABLE IF NOT EXISTS phones (phone TEXT PRIMARY KEY, entities INTEGER);
        """
    )


def record_snapshot(db: sqlite3.Connection, name: str, url: str, path: Path, rows: int) -> None:
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)", (name, url, sha256_file(path), utc_now(), rows))


def canonical(value: object) -> tuple[str, str]:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_roles(db: sqlite3.Connection, path: Path, universe: set[str]) -> int:
    rows = 0
    for item in iter_json_array(path):
        org = str(item.get("organisasjonsnummer") or "")
        if org not in universe:
            continue
        body, digest = canonical(normalize_roles(item))
        db.execute("INSERT OR REPLACE INTO roles VALUES (?, ?, ?)", (org, body, digest))
        rows += 1
    record_snapshot(db, "roles", ROLES_URL, path, rows)
    return rows


def load_locations(db: sqlite3.Connection, path: Path, universe: set[str]) -> int:
    grouped: dict[str, list[dict[str, object]]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parent = row.get("overordnetEnhet") or ""
            if parent not in universe or row.get("nedleggelsesdato"):
                continue
            grouped.setdefault(parent, []).append({
                "organisation_number": row.get("organisasjonsnummer"),
                "name": row.get("navn"),
                "address": {
                    "adresse": [part for part in (row.get("beliggenhetsadresse.adresse") or "").split("\n") if part],
                    "postnummer": row.get("beliggenhetsadresse.postnummer") or None,
                    "poststed": row.get("beliggenhetsadresse.poststed") or None,
                    "kommune": row.get("beliggenhetsadresse.kommune") or None,
                    "kommunenummer": row.get("beliggenhetsadresse.kommunenummer") or None,
                    "landkode": row.get("beliggenhetsadresse.landkode") or None,
                },
                "industry": {"kode": row.get("naeringskode1.kode") or None, "beskrivelse": row.get("naeringskode1.beskrivelse") or None},
                "employees": int(row["antallAnsatte"]) if (row.get("antallAnsatte") or "").isdigit() else None,
                "website": row.get("hjemmeside") or None,
            })
    for org, items in grouped.items():
        items.sort(key=lambda item: str(item["organisation_number"]))
        body, digest = canonical({"locations": items})
        db.execute("INSERT OR REPLACE INTO locations VALUES (?, ?, ?)", (org, body, digest))
    record_snapshot(db, "locations", SUBUNITS_URL, path, len(grouped))
    return len(grouped)


def load_shared_identifiers(db: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Share counts over the whole registry, so administrator domains and phones are recognised."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        rows = [{"epostadresse": row.get("epostadresse"), "telefon": row.get("telefon"), "mobil": row.get("mobil")} for row in csv.DictReader(handle)]
    emails = email_domain_counts(rows)
    phones = phone_share_counts(rows)
    db.executemany("INSERT OR REPLACE INTO email_domains VALUES (?, ?)", emails.items())
    db.executemany("INSERT OR REPLACE INTO phones VALUES (?, ?)", phones.items())
    record_snapshot(db, "shared_identifiers", ENTITIES_URL, path, len(rows))
    return len(emails), len(phones)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", required=True)
    parser.add_argument("--roles", required=True, help="roller-totalbestand.json.gz")
    parser.add_argument("--subunits", required=True, help="underenheter.csv.gz")
    parser.add_argument("--entities", required=True, help="enheter bulk CSV (gzip)")
    parser.add_argument("--output", required=True, help="SQLite cache path")
    args = parser.parse_args()

    universe = universe_numbers(Path(args.universe))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output) as db:
        create_schema(db)
        shared_domains, shared_phones = load_shared_identifiers(db, Path(args.entities))
        summary = {
            "universe": len(universe),
            "roles": load_roles(db, Path(args.roles), universe),
            "locations": load_locations(db, Path(args.subunits), universe),
            "email_domains": shared_domains,
            "phones": shared_phones,
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
