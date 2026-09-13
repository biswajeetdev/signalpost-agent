import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import BudgetExhausted  # noqa: E402
from norway_company_agent.site_discovery import Page, contact_links, discover_website  # noqa: E402

ROW = {
    "organisasjonsnummer": "985589003",
    "navn": "FJELD OG VANN AS",
    "epostadresse": "post@bate.no",
    "telefon": "57 69 89 50",
    "hjemmeside": "",
}
SHARED = {"bate.no": 520, "vestbo.no": 632}


class FakeWeb:
    def __init__(self, pages: dict[str, Page], hosts: set[str]) -> None:
        self.pages = pages
        self.hosts = hosts
        self.calls: list[str] = []

    def fetch(self, url: str) -> Page:
        self.calls.append(url)
        return self.pages.get(url) or Page(url, None, 404, error="HTTP 404")

    def resolve(self, host: str) -> bool:
        return host in self.hosts


def page(url: str, html: str, final_url: str | None = None) -> Page:
    return Page(url, final_url or url, 200, html, "sha-" + url, "2026-09-13T08:00:00Z")


def run(web: FakeWeb, **kwargs):
    return discover_website(
        ROW,
        shared_domains=SHARED,
        shared_phones={},
        fetch=web.fetch,
        robots_allowed=lambda url: True,
        resolver=web.resolve,
        **kwargs,
    )


class SiteDiscoveryTest(unittest.TestCase):
    def test_administrator_domain_is_skipped_and_guess_with_phone_proof_is_published(self) -> None:
        web = FakeWeb(
            {
                "https://bate.no/": page("https://bate.no/", "Våre sameier: Fjeld og Vann AS, org 985 589 003"),
                "https://fjeldogvann.no/": page("https://fjeldogvann.no/", '<a href="tel:+4757698950">Ring oss</a>'),
            },
            {"bate.no", "fjeldogvann.no"},
        )
        record, home = run(web)
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"]["candidate_source"], "name_guess")
        self.assertEqual(record["value"]["identity_assessment"]["proofs"], ["registry_phone"])
        self.assertEqual(record["attempts"][0]["outcome"], "related")
        self.assertEqual(home.final_url, "https://fjeldogvann.no/")

    def test_proof_found_on_linked_contact_page(self) -> None:
        web = FakeWeb(
            {
                "https://www.fjeldogvann.no/": page("https://www.fjeldogvann.no/", '<a href="/nyheter">Nytt</a><a href="/kontakt-oss">Kontakt</a>'),
                "https://www.fjeldogvann.no/kontakt-oss": page("https://www.fjeldogvann.no/kontakt-oss", "<footer>Org.nr. 985589003</footer>"),
            },
            {"www.fjeldogvann.no"},
        )
        record, _ = run(web)
        self.assertEqual(record["status"], "available")
        self.assertEqual([item["proofs"] for item in record["value"]["proof_pages"]], [[], ["organisation_number"]])
        self.assertNotIn("https://www.fjeldogvann.no/nyheter", web.calls)

    def test_redirect_into_shared_administrator_domain_is_not_published(self) -> None:
        web = FakeWeb(
            {"https://fjeldvann.no/": page("https://fjeldvann.no/", "Org 985589003", final_url="https://www.vestbo.no/")},
            {"fjeldvann.no"},
        )
        record, home = run(web)
        self.assertEqual(record["status"], "ambiguous")
        self.assertFalse(record["value"]["identity_assessment"]["publishable"])
        self.assertIsNone(home)

    def test_no_resolving_candidate_makes_no_requests(self) -> None:
        web = FakeWeb({}, set())
        record, _ = run(web)
        self.assertEqual(record["status"], "not_available")
        self.assertEqual(web.calls, [])
        self.assertTrue(all(item["outcome"] == "no_dns" for item in record["attempts"]))

    def test_budget_exhaustion_is_reported_as_failed(self) -> None:
        def exhausted(url: str) -> Page:
            raise BudgetExhausted("company allowance exhausted (14)")

        record, _ = discover_website(ROW, shared_domains=SHARED, shared_phones={}, fetch=exhausted, robots_allowed=lambda url: True, resolver=lambda host: True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("allowance", record["note"])

    def test_host_cap_limits_homepage_fetches(self) -> None:
        web = FakeWeb({}, set())
        web.resolve = lambda host: not host.startswith("www.")  # type: ignore[method-assign]
        web.pages = {f"https://{label}/": page(f"https://{label}/", "no identifiers here") for label in ("bate.no", "fjeldvann.no", "fjeld-vann.no", "vann.no", "fjeld.no", "fjeldvann.com")}
        record, _ = run(web, max_hosts=2)
        self.assertEqual(len(web.calls), 2)
        self.assertEqual(record["status"], "ambiguous")

    def test_contact_links_stay_on_host_and_rank_contact_first(self) -> None:
        html = '<a href="https://other.no/kontakt">x</a><a href="/om-oss">Om</a><a href="/kontakt">Kontakt</a><a href="/">Hjem</a>'
        self.assertEqual(contact_links("https://firma.no/", html), ["https://firma.no/kontakt", "https://firma.no/om-oss"])


if __name__ == "__main__":
    unittest.main()
