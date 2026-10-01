import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import search_candidates  # noqa: E402
from norway_company_agent.candidates import website_candidates  # noqa: E402


class SearchCandidatesTest(unittest.TestCase):
    def test_dormant_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(search_candidates.search_key())

    def test_directories_are_dropped_and_domains_capped(self):
        payload = {"web": {"results": [{"url": u} for u in (
            "https://www.proff.no/selskap/acme", "https://www.facebook.com/acme", "https://acme-bygg.no/kontakt",
            "https://www.acmebygg.com/", "https://kart.gulesider.no/x", "https://third.no", "https://fourth.no")]}}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        spent = []
        with mock.patch("urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())):
            domains = search_candidates.search_domains({"navn": "ACME BYGG AS", "forretningsadresse.poststed": "OSLO"}, "k", spend=lambda: spent.append(1))
        self.assertEqual(domains, ["acme-bygg.no", "acmebygg.com", "third.no"])
        self.assertEqual(spent, [1])

    def test_failure_returns_nothing(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(search_candidates.search_domains({"navn": "ACME AS"}, "k"), [])

    def test_search_domains_become_candidates(self):
        sources = {c["domain"]: c["source"] for c in website_candidates({"navn": "ACME BYGG AS", "_search_domains": ["acme-bygg.no"]}, {})}
        self.assertEqual(sources["acme-bygg.no"], "search_result")


if __name__ == "__main__":
    unittest.main()
