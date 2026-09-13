import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_cached_official import build_cache  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.cached_official import OfficialCache  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.pipeline import RunSettings, run_batch  # noqa: E402
from norway_company_agent.site_discovery import Page  # noqa: E402

ACCOUNTS = [{
    "id": 7, "regnskapstype": "SELSKAP", "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK",
    "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 4200000.0}, "driftsresultat": 250000.0}, "aarsresultat": 190000.0},
    "eiendeler": {"sumEiendeler": 2100000.0},
    "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 800000.0}, "gjeldOversikt": {"sumGjeld": 1300000.0}},
}]
HOME = (
    '<html><title>Arkitektfirma Jon Vikøren</title><footer>Org.nr 985 589 003 '
    '<a href="https://www.facebook.com/arkjv">Facebook</a> <a href="https://www.facebook.com/sharer.php?u=x">Del</a></footer></html>'
)


def profile(org: str, name: str, email: str = "", website: str = "") -> dict:
    row = {"organisasjonsnummer": org, "navn": name, "epostadresse": email, "telefon": "", "hjemmeside": website}
    return {"organisation_number": org, "name": name, "evidence": {"registry": evidence(
        "registry", "available", "official_registry_bulk", "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
        value=row, retrieved_at="2026-09-13T07:59:43Z", content_sha256="ee" * 32, source_row_key=org)}}


def official_fetcher(budget: RequestBudget, company: str):
    def fetch(url: str) -> FetchResult:
        budget.spend(company, "official", essential=True)
        if company == "999999999":
            raise RuntimeError("registry connection reset")
        if url.endswith("/aar"):
            return FetchResult(url, 200, 5, 10, ["2024", "2025"], content_sha256="f1" * 32, retrieved_at="2026-09-13T08:00:00Z")
        if company == "985589003":
            return FetchResult(url, 200, 5, 10, ACCOUNTS, content_sha256="f2" * 32, retrieved_at="2026-09-13T08:00:00Z")
        return FetchResult(url, 404, 5, 0, error="HTTP 404", retrieved_at="2026-09-13T08:00:00Z")

    return fetch


def site_fetchers(budget: RequestBudget, company: str, *, allowance: int, robots):
    pages = {"https://www.arkjv.no/": Page("https://www.arkjv.no/", "https://www.arkjv.no/", 200, HOME, "ab" * 32, "2026-09-13T08:00:01Z")}

    def fetch(url: str) -> Page:
        budget.spend(company, "site_page", allowance=allowance)
        return pages.get(url) or Page(url, None, 404, error="HTTP 404")

    return fetch, lambda url: True


def resolver(host: str) -> bool:
    return host == "www.arkjv.no"


class PipelineTest(unittest.TestCase):
    def run_batch(self, profiles, budget=None):
        return run_batch(
            profiles,
            cache=OfficialCache(build_cache()),
            budget=budget or RequestBudget(2000, 2700),
            run_id="test-run",
            settings=RunSettings(workers=2),
            official_fetcher=official_fetcher,
            site_fetchers=site_fetchers,
            resolver=resolver,
        )

    def test_end_to_end_envelopes_validate_and_cover_every_module(self) -> None:
        inputs = [profile("985589003", "ARKITEKTFIRMA JON VIKØREN AS", email="post@arkjv.no"), profile("912345678", "UKJENT HOLDING AS")]
        envelopes, profiles, report = self.run_batch(inputs)
        self.assertEqual([item["organisation_number"] for item in envelopes], ["985589003", "912345678"])
        self.assertTrue(report["validation"]["passed"], report["validation"])
        first, second = envelopes
        fields = {claim["field"]: claim for claim in first["claims"] if claim["availability"] == "available"}
        self.assertEqual(fields["official_website"]["value"], "https://www.arkjv.no/")
        self.assertEqual(fields["financials.revenue"]["value"], {"amount": 4200000.0, "currency": "NOK"})
        self.assertEqual(fields["financials.filed_years"]["value"], ["2024", "2025"])
        self.assertEqual(fields["role"]["value"]["name"], "Kari Nordmann")
        socials = [claim["value"] for claim in first["claims"] if claim["field"] == "social_profile"]
        self.assertEqual(socials, [{"platform": "facebook", "url": "https://facebook.com/arkjv"}])
        self.assertEqual(first["modules"]["locations"], "not_available")
        self.assertEqual(second["modules"]["website"], "not_available")
        self.assertEqual(second["modules"]["social_profiles"], "not_available")
        self.assertEqual(second["modules"]["financials"], "not_available")
        self.assertGreater(first["operations"]["requests"], 0)
        self.assertEqual(report["operations"]["requests"], sum(item["operations"]["requests"] for item in envelopes))

    def test_unexpected_error_still_emits_a_valid_failed_envelope(self) -> None:
        envelopes, _, report = self.run_batch([profile("999999999", "FEIL AS")])
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0]["modules"]["financials"], "failed")
        self.assertTrue(any("connection reset" in error["message"] for error in envelopes[0]["errors"]))
        self.assertTrue(report["validation"]["passed"], report["validation"])

    def test_discovery_is_skipped_near_the_deadline(self) -> None:
        envelopes, _, _ = self.run_batch([profile("985589003", "ARKITEKTFIRMA JON VIKØREN AS")], budget=RequestBudget(2000, 60))
        self.assertEqual(envelopes[0]["modules"]["website"], "failed")
        self.assertIn("wall-clock", envelopes[0]["errors"][-1]["message"])

    def test_refresh_against_previous_profiles_reports_changes_and_is_idempotent(self) -> None:
        inputs = [profile("985589003", "ARKITEKTFIRMA JON VIKØREN AS", email="post@arkjv.no")]
        _, first_profiles, _ = self.run_batch(inputs)
        previous = {item["organisation_number"]: item for item in first_profiles}
        rerun = run_batch([profile("985589003", "ARKITEKTFIRMA JON VIKØREN AS", email="post@arkjv.no")], cache=OfficialCache(build_cache()), budget=RequestBudget(2000, 2700), run_id="test-run-2", settings=RunSettings(workers=1), previous=previous, official_fetcher=official_fetcher, site_fetchers=site_fetchers, resolver=resolver)
        self.assertEqual(rerun[0][0]["changes"], [])


if __name__ == "__main__":
    unittest.main()
