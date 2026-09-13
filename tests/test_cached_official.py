import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_official_cache import canonical, create_schema  # noqa: E402
from norway_company_agent.cached_official import OfficialCache  # noqa: E402

ROLES = {"roles": [{"name": "Kari Nordmann", "role_code": "DAGL", "role": "Daglig leder", "inactive": False}]}


def build_cache() -> Path:
    path = Path(tempfile.mkdtemp()) / "official.sqlite"
    with sqlite3.connect(path) as db:
        create_schema(db)
        for name in ("roles", "locations", "shared_identifiers"):
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)", (name, f"https://data.brreg.no/{name}", "ab" * 32, "2026-09-13T07:37:00Z", "2026-09-13T07:43:00Z", 1))
        body, digest = canonical(ROLES)
        db.execute("INSERT INTO roles VALUES (?, ?, ?)", ("985589003", body, digest))
        db.execute("INSERT INTO email_domains VALUES (?, ?)", ("bate.no", 520))
        db.execute("INSERT INTO phones VALUES (?, ?)", ("57698950", 7))
        db.execute("INSERT INTO name_keys VALUES (?, ?)", ("arkitektfirma jon vikoren", 1))
    return path


class OfficialCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = OfficialCache(build_cache())

    def test_available_record_cites_entity_url_snapshot_time_and_hash(self) -> None:
        record = self.cache.module_records("985589003")["roles"]
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"], ROLES)
        self.assertEqual(record["source_url"], "https://data.brreg.no/enhetsregisteret/api/enheter/985589003/roller")
        self.assertEqual(record["retrieved_at"], "2026-09-13T07:37:00Z")
        self.assertEqual(record["content_sha256"], canonical(ROLES)[1])
        self.assertIn("Declared bulk snapshot", record["note"])

    def test_missing_row_is_not_available_never_empty_value(self) -> None:
        record = self.cache.module_records("985589003")["locations"]
        self.assertEqual(record["status"], "not_available")
        self.assertIsNone(record["value"])

    def test_module_filter(self) -> None:
        self.assertEqual(set(self.cache.module_records("985589003", {"roles"})), {"roles"})

    def test_share_counts(self) -> None:
        self.assertEqual(self.cache.shared_domains().get("bate.no"), 520)
        self.assertEqual(self.cache.shared_domains().get("unknown.no", 0), 0)
        self.assertEqual(self.cache.shared_phones().get("57698950"), 7)
        self.assertEqual(self.cache.name_keys().get("arkitektfirma jon vikoren"), 1)

    def test_cache_path_must_exist(self) -> None:
        with self.assertRaises(FileNotFoundError):
            OfficialCache(Path(tempfile.mkdtemp()) / "missing.sqlite")


if __name__ == "__main__":
    unittest.main()
