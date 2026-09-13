import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_official_cache import iter_json_array  # noqa: E402


class IterJsonArrayTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "items.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_objects_split_across_small_chunks(self) -> None:
        items = [{"organisasjonsnummer": str(900000000 + index), "navn": "Ærlig Øl & Å", "rollegrupper": [{"roller": []}]} for index in range(25)]
        path = self.write(json.dumps(items, ensure_ascii=False, indent=2))
        self.assertEqual(list(iter_json_array(path, chunk_size=7)), items)

    def test_empty_array(self) -> None:
        self.assertEqual(list(iter_json_array(self.write("  [ ]  "), chunk_size=3)), [])

    def test_non_array_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            list(iter_json_array(self.write('{"a": 1}')))


if __name__ == "__main__":
    unittest.main()
