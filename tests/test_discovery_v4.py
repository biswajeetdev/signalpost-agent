import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.candidates import website_candidates  # noqa: E402
from norway_company_agent.pipeline import with_subunits  # noqa: E402
from norway_company_agent.proof import page_proof_spans, registry_identifiers  # noqa: E402
from norway_company_agent.site_discovery import Page, contact_links, discover_website  # noqa: E402

ROW = {"organisasjonsnummer": "912345678", "navn": "ACME HOLDING AS", "epostadresse": "", "telefon": "", "mobil": ""}
LOCATIONS = {"status": "available", "value": {"locations": [
    {"organisation_number": "811111111", "name": "ACME KAFE", "website": "www.acmekafe.no"},
]}}


class SubunitTest(unittest.TestCase):
    def test_subunit_number_is_a_strong_proof(self):
        row = with_subunits(ROW, LOCATIONS)
        spans = page_proof_spans(registry_identifiers(row), "<footer>Acme Kafe · Org.nr 811 111 111</footer>")
        self.assertIn("subunit_organisation_number", spans)
        self.assertNotIn("organisation_number", spans)

    def test_subunit_websites_and_names_become_candidates(self):
        sources = {item["domain"]: item["source"] for item in website_candidates(with_subunits(ROW, LOCATIONS), {})}
        self.assertEqual(sources["acmekafe.no"], "subunit_registry_website")
        self.assertIn("subunit_registry_website", list(sources.values())[:2])

    def test_no_locations_leaves_row_unchanged(self):
        self.assertEqual(with_subunits(ROW, {"status": "not_available"}), ROW)


class ProofPagesTest(unittest.TestCase):
    def test_legal_page_is_not_crowded_out(self):
        html = """<a href="/kontakt">Kontakt</a><a href="/kontakt-oss">Kontakt oss</a><a href="/contact">Contact</a>
                  <a href="/om-oss">Om oss</a><a href="/personvern">Personvern</a>"""
        links = contact_links("https://acme.no/", html)
        self.assertEqual(len(links), 3)
        self.assertIn("https://acme.no/personvern", links)
        self.assertIn("https://acme.no/om-oss", links)


class FallbackTest(unittest.TestCase):
    def test_falls_back_to_www_then_http(self):
        tried = []

        def robots(url):
            tried.append(url)
            return True if url == "http://www.acmeholding.no/" else None

        def fetch(url):
            return Page(url, url, 200, "<p>ACME HOLDING AS org.nr 912 345 678</p>", "a" * 64, "t")

        record, _ = discover_website(ROW, shared_domains={}, shared_phones={}, fetch=fetch, robots_allowed=robots,
                                     resolver=lambda host: host in {"acmeholding.no", "www.acmeholding.no"})
        self.assertEqual(record["status"], "available")
        self.assertEqual(tried[:4], ["https://acmeholding.no/", "https://www.acmeholding.no/", "http://acmeholding.no/", "http://www.acmeholding.no/"])


if __name__ == "__main__":
    unittest.main()
