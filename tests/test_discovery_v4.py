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


class CheapFallbackTest(unittest.TestCase):
    def _run(self, reason):
        tried = []

        def robots(url):
            tried.append(url)
            return True if url.startswith("http://") else None

        robots.unreachable_reason = lambda url: reason
        fetch = lambda url: Page(url, url, 200, "<p>ACME HOLDING AS org.nr 912 345 678</p>", "a" * 64, "t")  # noqa: E731
        record, _ = discover_website(ROW, shared_domains={}, shared_phones={}, fetch=fetch, robots_allowed=robots,
                                     resolver=lambda host: host in {"acmeholding.no", "www.acmeholding.no"}, max_hosts=1)
        return tried, record

    def test_timeout_stops_trying_variants(self):
        tried, record = self._run("URLError: <urlopen error timed out>")
        self.assertEqual(tried[0], "https://acmeholding.no/")
        self.assertFalse(any(url.startswith("http://") for url in tried))
        self.assertNotIn("https://www.acmeholding.no/", tried)

    def test_tls_failure_falls_back_to_http(self):
        tried, record = self._run("URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]>")
        self.assertIn("http://acmeholding.no/", tried)
        self.assertEqual(record["status"], "available")


class PrecisionGateTest(unittest.TestCase):
    """Cases from the audit of the v3 1,000-company run, where the unique-name rule published wrong sites."""

    def _discover(self, name, domain, html, org="912345678"):
        row = {"organisasjonsnummer": org, "navn": name, "epostadresse": "", "telefon": "", "mobil": ""}
        from norway_company_agent.proof import name_key
        keys = {name_key(name): 1}
        fetch = lambda url: Page(url, url, 200, html, "a" * 64, "t")  # noqa: E731
        record, _ = discover_website(row, shared_domains={}, shared_phones={}, fetch=fetch, robots_allowed=lambda url: True,
                                     resolver=lambda host: host in {domain, "www." + domain}, name_keys=keys, max_hosts=6)
        return record

    LONG = "<p>" + "Velkommen til oss. Vi leverer gode tjenester i hele regionen. " * 60 + "</p>"

    def test_parked_page_is_never_published(self):
        record = self._discover("VERAX HOLDING AS", "verax.no", "<p>verax.no is parked. verax.no is registered, but the owner has not set up a site.</p>")
        self.assertNotEqual(record["status"], "available")
        self.assertIn("parked", [a["outcome"] for a in record["attempts"]])

    def test_domain_mention_is_not_the_legal_name_outside_no(self):
        record = self._discover("BRANDSUITE AS", "brandsuite.com", "<p>www.brandsuite.com</p>" + self.LONG)
        self.assertNotEqual(record["status"], "available")

    def test_com_name_rule_needs_registered_address(self):
        row_extra = {"forretningsadresse.adresse": "Storgata 12", "forretningsadresse.postnummer": "0155"}
        page = "<h1>Norus Renewables AS</h1><p>Storgata 12, 0155 Oslo</p>" + self.LONG
        from norway_company_agent.proof import name_key
        row = {"organisasjonsnummer": "912345678", "navn": "NORUS RENEWABLES AS", "epostadresse": "", "telefon": "", "mobil": "", **row_extra}
        fetch = lambda url: Page(url, url, 200, page, "a" * 64, "t")  # noqa: E731
        record, _ = discover_website(row, shared_domains={}, shared_phones={}, fetch=fetch, robots_allowed=lambda url: True,
                                     resolver=lambda host: host in {"norusrenewables.com"}, name_keys={name_key(row["navn"]): 1}, max_hosts=6)
        self.assertEqual(record["status"], "available")
        self.assertIn("registry_address", record["value"]["identity_assessment"]["proofs"])
        without = self._discover("NORUS RENEWABLES AS", "norusrenewables.com", "<h1>Norus Renewables AS</h1>" + self.LONG)
        self.assertNotEqual(without["status"], "available")

    def test_name_rule_does_not_apply_to_com(self):
        record = self._discover("DUERTEX AS", "duertex.com", "<h1>DUERTEX</h1><p>The worlds most comfortable pants</p>" + self.LONG)
        self.assertNotEqual(record["status"], "available")

    def test_acronym_needs_an_identifier(self):
        record = self._discover("BQL AS", "bql.no", "<h1>BQL</h1><p>Bergen Quiltelag</p>" + self.LONG)
        self.assertNotEqual(record["status"], "available")

    def test_com_name_rule_accepts_norwegian_contact_tie(self):
        record = self._discover("NORUS RENEWABLES AS", "norusrenewables.com", "<h1>Norus Renewables AS</h1><p>Ring oss: +47 22 33 44 55</p>" + self.LONG)
        self.assertEqual(record["status"], "available")
        self.assertIn("norway_contact", record["value"]["identity_assessment"]["proofs"])
        lang = self._discover("NORUS RENEWABLES AS", "norusrenewables.com", '<html lang="nb"><h1>Norus Renewables AS</h1>' + self.LONG)
        self.assertEqual(lang["status"], "available")

    def test_com_name_rule_rejects_foreign_contact(self):
        record = self._discover("DUERTEX AS", "duertex.com", '<html lang="en"><h1>DUERTEX</h1><p>Call +1 604 555 0101, hello@duertex.com</p>' + self.LONG)
        self.assertNotEqual(record["status"], "available")

    def test_foreign_org_number_blocks_name_rule(self):
        record = self._discover("PDIMPORT AS", "pdimport.no", "<p>PDIMPORT AS Org. nr. 930692522</p>" + self.LONG)
        self.assertNotEqual(record["status"], "available")

    def test_unique_long_no_name_still_publishes(self):
        record = self._discover("RELASJONSPSYKOLOGEN AS", "relasjonspsykologen.no", "<h1>Relasjonspsykologen AS</h1>" + self.LONG)
        self.assertEqual(record["status"], "available")

    def test_own_org_number_still_publishes_despite_other_numbers(self):
        record = self._discover("ACME HOLDING AS", "acmeholding.no", "<p>Acme Holding AS org.nr 912 345 678. Datter: org.nr 999 888 777</p>" + self.LONG)
        self.assertEqual(record["status"], "available")


if __name__ == "__main__":
    unittest.main()
