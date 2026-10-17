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

    def test_redirect_to_another_business_that_never_names_us_is_not_published(self) -> None:
        web = FakeWeb(
            {"https://fjeldogvann.no/": page("https://fjeldogvann.no/", "Butikk. Tel: +47 57 69 89 50", final_url="https://shop.annet.no/nb-no/fv")},
            {"fjeldogvann.no"},
        )
        record, home = run(web)
        self.assertEqual(record["status"], "ambiguous")
        self.assertIsNone(home)

    def test_redirect_to_a_new_domain_that_names_us_is_published(self) -> None:
        web = FakeWeb(
            {"https://fjeldogvann.no/": page("https://fjeldogvann.no/", "Fjeld og Vann AS. Tel: +47 57 69 89 50", final_url="https://fjeldvann.no/")},
            {"fjeldogvann.no"},
        )
        record, _ = run(web)
        self.assertEqual(record["status"], "available")

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

    def test_registry_declared_site_with_legal_name_publishes_without_contact_fetch(self) -> None:
        row = {**ROW, "hjemmeside": "www.gamlefirma.no", "epostadresse": ""}
        web = FakeWeb({"https://gamlefirma.no/": page("https://gamlefirma.no/", '<title>Fjeld og Vann - rør</title><a href="/kontakt">Kontakt</a>')}, {"gamlefirma.no"})
        record, _ = discover_website(row, shared_domains=SHARED, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: True, resolver=web.resolve)
        self.assertEqual(record["status"], "available")
        self.assertIn("registry_declared_website", record["value"]["identity_assessment"]["proofs"])
        self.assertIn("Fjeld og Vann", record["value"]["proof_pages"][0]["claim_spans"]["legal_name_on_site"])
        self.assertEqual(web.calls, ["https://gamlefirma.no/"])

    def test_bot_challenge_page_is_reported_as_unchecked_not_as_no_official_tie(self) -> None:
        row = {**ROW, "hjemmeside": "www.gamlefirma.no", "epostadresse": ""}
        challenge = '<title>One moment, please...</title><div id="spinner"></div><p>Please wait while your request is being verified...</p>'
        web = FakeWeb({"https://gamlefirma.no/": page("https://gamlefirma.no/", challenge)}, {"gamlefirma.no"})
        record, _ = discover_website(row, shared_domains=SHARED, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: True, resolver=web.resolve)
        self.assertEqual(record["status"], "failed")
        self.assertIn("gamlefirma.no served a bot-protection check", record["note"])
        self.assertIn("bot_challenge", {item["outcome"] for item in record["attempts"]})

    def test_unreachable_host_is_skipped_without_page_fetch(self) -> None:
        web = FakeWeb({}, {"fjeldvann.no", "fjeldogvann.no"})
        record, _ = discover_website(ROW, shared_domains=SHARED, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: None, resolver=web.resolve)
        self.assertEqual(record["status"], "not_available")
        self.assertEqual(web.calls, [])
        self.assertIn("unreachable", {item["outcome"] for item in record["attempts"]})

    def test_group_domain_is_exact_for_parent_but_related_for_subsidiary(self) -> None:
        shared = {"afgruppen.no": 40}
        html = "<footer>AF Gruppen ASA · Org.nr 938 702 675 · Org.nr AF Anlegg 912 345 678</footer>"
        web = FakeWeb({"https://afgruppen.no/": page("https://afgruppen.no/", html)}, {"afgruppen.no"})
        parent = {"organisasjonsnummer": "938702675", "navn": "AF GRUPPEN ASA", "epostadresse": "post@afgruppen.no", "telefon": "", "hjemmeside": ""}
        record, _ = discover_website(parent, shared_domains=shared, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: True, resolver=web.resolve)
        self.assertEqual(record["status"], "available")
        child = {"organisasjonsnummer": "912345678", "navn": "AF ANLEGG AS", "epostadresse": "post@afgruppen.no", "telefon": "", "hjemmeside": ""}
        record, _ = discover_website(child, shared_domains=shared, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: True, resolver=web.resolve)
        self.assertEqual(record["status"], "ambiguous")

    def test_unique_name_rule_is_off_by_default_and_needs_full_name_domain(self) -> None:
        # The name in running text only, so this exercises the domain rule and not the masthead rule.
        body = "<title>Rørlegger i Sogn</title><p>Velkommen til Fjeld og Vann AS.</p>"
        web = FakeWeb({"https://fjeldvann.no/": page("https://fjeldvann.no/", body), "https://vann.no/": page("https://vann.no/", body)}, {"fjeldvann.no"})
        record, _ = run(web)
        self.assertEqual(record["status"], "ambiguous")
        record, _ = run(web, name_keys={"fjeld vann": 1})
        self.assertEqual(record["status"], "available")
        self.assertIn("unique_legal_name_domain", record["value"]["identity_assessment"]["proofs"])
        record, _ = run(web, name_keys={"fjeld vann": 2})
        self.assertEqual(record["status"], "ambiguous")
        # A partial-name domain never publishes on the name in running text (a customer, partner or parent page).
        partial_only = FakeWeb(web.pages, {"vann.no"})
        record, _ = run(partial_only, name_keys={"fjeld vann": 1})
        self.assertEqual(record["status"], "ambiguous")

    def test_masthead_name_publishes_and_an_as_name_needs_no_namesake_check(self) -> None:
        title = "<title>Fjeld og Vann AS - rørlegger</title>"
        masthead = FakeWeb({"https://vann.no/": page("https://vann.no/", title)}, {"vann.no"})
        for keys in (None, {"fjeld vann": 1}, {"fjeld vann": 2}):
            record, _ = run(masthead, name_keys=keys)
            self.assertEqual(record["status"], "available", keys)
            self.assertIn("legal_name_masthead", record["value"]["identity_assessment"]["proofs"])

    def test_masthead_for_other_forms_needs_a_registry_unique_name(self) -> None:
        row = {**ROW, "navn": "FJELD OG VANN DA"}
        web = FakeWeb({"https://vann.no/": page("https://vann.no/", "<title>Fjeld og Vann DA - rørlegger</title>")}, {"vann.no"})
        def go(keys):
            return discover_website(row, shared_domains=SHARED, shared_phones={}, fetch=web.fetch, robots_allowed=lambda url: True,
                                    resolver=web.resolve, name_keys=keys)[0]["status"]
        self.assertEqual(go({"fjeld vann": 2}), "ambiguous")
        self.assertEqual(go({"fjeld vann": 1}), "available")

    def test_redirect_to_another_domain_publishes_only_when_it_names_us_in_the_masthead(self) -> None:
        named = FakeWeb({"https://fjeldvann.no/": page("https://fjeldvann.no/", "<title>Fjeld og Vann AS</title>", "https://fv-ror.no/")}, {"fjeldvann.no"})
        record, _ = run(named)
        self.assertEqual(record["status"], "available")
        shop = FakeWeb({"https://fjeldvann.no/": page("https://fjeldvann.no/", "<title>Captech nettbutikk</title><p>Tlf 57 69 89 50</p>", "https://captech.no/")}, {"fjeldvann.no"})
        record, _ = run(shop)
        self.assertNotEqual(record["status"], "available")

    def test_contact_links_stay_on_host_and_rank_contact_first(self) -> None:
        html = '<a href="https://other.no/kontakt">x</a><a href="/om-oss">Om</a><a href="/kontakt">Kontakt</a><a href="/">Hjem</a>'
        self.assertEqual(contact_links("https://firma.no/", html), ["https://firma.no/kontakt", "https://firma.no/om-oss"])


if __name__ == "__main__":
    unittest.main()
