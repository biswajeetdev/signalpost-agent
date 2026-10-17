import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.contract import availability, build_envelope, validate_envelope  # noqa: E402

AT = "2026-09-13T08:00:00Z"
PROFILE = {
    "organisation_number": "985589003",
    "name": "ARKITEKTFIRMA JON VIKØREN AS",
    "evidence": {
        "registry": {
            "field": "registry", "status": "available", "source_class": "official_registry_bulk",
            "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv", "retrieved_at": AT,
            "content_sha256": "aa" * 32, "source_row_key": "985589003",
            "value": {
                "navn": "ARKITEKTFIRMA JON VIKØREN AS", "organisasjonsform.kode": "AS", "naeringskode1.kode": "71.110",
                "naeringskode1.beskrivelse": "Arkitektvirksomhet", "antallAnsatte": "", "hjemmeside": "www.arkjv.no",
                "forretningsadresse.adresse": "Tomtebu 2", "forretningsadresse.postnummer": "6893",
                "forretningsadresse.poststed": "VIK I SOGN", "forretningsadresse.kommune": "VIK", "konkurs": "false",
            },
        },
        "financials": {
            "field": "financials", "status": "available", "source_class": "official_annual_accounts",
            "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/985589003", "retrieved_at": AT, "content_sha256": "bb" * 32,
            "value": {"records": [
                {"record_id": 7, "account_type": "SELSKAP", "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "currency": "NOK",
                 "revenue": 4200000.0, "operating_result": 0.0, "profit_before_tax": None, "annual_result": 310000.0, "assets": 2100000.0, "equity": None, "debt": 900000.0},
                {"record_id": 6, "account_type": "SELSKAP", "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "currency": "NOK", "revenue": 1.0},
            ]},
        },
        "roles": {
            "field": "roles", "status": "available", "source_class": "official_roles_bulk_snapshot",
            "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/985589003/roller", "retrieved_at": AT, "content_sha256": "cc" * 32,
            "value": {"roles": [
                {"name": "Jon Vikøren", "role_code": "DAGL", "role": "Daglig leder", "inactive": False, "last_changed": "2019-02-01"},
                {"name": "Old Director", "role_code": "MEDL", "role": "Styremedlem", "inactive": True},
            ]},
        },
        "locations": {"field": "locations", "status": "not_found", "source_class": "official_subunits", "source_url": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=985589003", "retrieved_at": AT, "note": "HTTP 404"},
        "website": {
            "field": "website", "status": "available", "source_class": "verified_company_website", "source_url": "https://arkjv.no/",
            "retrieved_at": AT, "content_sha256": "dd" * 32, "method": "registry_candidates_with_site_proof_v2",
            "value": {
                "final_url": "https://arkjv.no/", "registered_domain": "arkjv.no", "candidate_source": "registry_website",
                "identity_assessment": {"status": "exact", "publishable": True, "proofs": ["legal_name_on_site", "organisation_number", "registry_declared_website"]},
                "proof_pages": [{"url": "https://arkjv.no/", "retrieved_at": AT, "content_sha256": "dd" * 32,
                                 "claim_spans": {"organisation_number": "Arkitektfirma Jon Vikøren AS Org.nr 985 589 003", "legal_name_on_site": "Arkitektfirma Jon Vikøren AS"}}],
            },
        },
    },
}


def envelope(profile=PROFILE):
    return build_envelope(profile, run_id="dev-1", started_at=AT, completed_at=AT, operations={"requests": 4, "runtime_ms": 900, "third_party_cost_usd": 0})


def claims(result, field):
    return [claim for claim in result["claims"] if claim["field"] == field]


class ContractTest(unittest.TestCase):
    def test_envelope_passes_validation(self) -> None:
        self.assertEqual(validate_envelope(envelope()), [])

    def test_missing_financial_value_is_not_available_and_zero_is_kept(self) -> None:
        result = envelope()
        equity = claims(result, "financials.equity")
        self.assertEqual(len(equity), 1)
        self.assertEqual((equity[0]["availability"], equity[0]["value"]), ("not_available", None))
        operating = claims(result, "financials.operating_result")[0]
        self.assertEqual(operating["value"], {"amount": 0.0, "currency": "NOK"})
        revenue = claims(result, "financials.revenue")
        self.assertEqual(len(revenue), 1, "only the latest reporting period is claimed")
        self.assertEqual(revenue[0]["reporting_period"], {"start": "2025-01-01", "end": "2025-12-31"})

    def test_empty_registry_employee_count_is_not_zero(self) -> None:
        employees = claims(envelope(), "registered_employees")[0]
        self.assertEqual((employees["availability"], employees["value"]), ("not_available", None))

    def test_only_active_roles_are_claimed(self) -> None:
        self.assertEqual([claim["value"]["name"] for claim in claims(envelope(), "role")], ["Jon Vikøren"])

    def test_website_claim_cites_proof_spans_and_registry_declaration(self) -> None:
        result = envelope()
        website = claims(result, "official_website")[0]
        self.assertEqual(website["confidence"], 0.99)
        evidence = {item["id"]: item for item in result["evidence"]}
        cited = [evidence[evidence_id] for evidence_id in website["evidence_ids"]]
        self.assertIn("Org.nr 985 589 003", {item["proof"]: item["claim_span"] for item in cited}["organisation_number"])
        self.assertEqual({item["proof"]: item["claim_span"] for item in cited}["registry_declared_website"], "hjemmeside: www.arkjv.no")

    def test_legacy_status_maps_to_contract_state(self) -> None:
        result = envelope()
        self.assertEqual(result["modules"]["locations"], "not_available")
        self.assertEqual(availability({"status": "source_error"}), "failed")

    def test_module_that_did_not_run_is_failed_with_error(self) -> None:
        profile = copy.deepcopy(PROFILE)
        del profile["evidence"]["financials"]
        result = envelope(profile)
        self.assertEqual(result["modules"]["financials"], "failed")
        self.assertIn({"module": "financials", "message": "Module did not run"}, result["errors"])
        self.assertEqual(validate_envelope(result), [])

    def test_rebuild_is_byte_identical(self) -> None:
        self.assertEqual(json.dumps(envelope(), sort_keys=True, ensure_ascii=False), json.dumps(envelope(copy.deepcopy(PROFILE)), sort_keys=True, ensure_ascii=False))

    def test_validation_catches_contract_violations(self) -> None:
        result = envelope()
        result["claims"].append({"field": "financials.revenue", "value": {"amount": 1}, "availability": "available", "evidence_ids": ["ev-missing"]})
        result["claims"].append({"field": "official_website", "value": "https://x.no/", "availability": "ambiguous", "evidence_ids": []})
        result["modules"]["roles"] = "complete"
        problems = validate_envelope(result)
        self.assertTrue(any("dangling" in problem for problem in problems))
        self.assertTrue(any("reporting period" in problem for problem in problems))
        self.assertTrue(any("carries a value" in problem for problem in problems))
        self.assertTrue(any("invalid state 'complete'" in problem for problem in problems))

    @staticmethod
    def _with_accounts(records, *, filed_years=None):
        profile = copy.deepcopy(PROFILE)
        profile["evidence"]["financials"]["value"]["records"] = records
        if filed_years:
            profile["evidence"]["financial_history"] = {
                "field": "financial_history", "status": "available", "source_class": "official_annual_accounts",
                "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/985589003/aar", "retrieved_at": AT,
                "content_sha256": "dd" * 32, "value": {"years": filed_years, "pdfs": []}}
        return envelope(profile)

    @staticmethod
    def _account(year, kind="SELSKAP", **figures):
        return {"record_id": f"{kind}{year}", "account_type": kind, "period": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"}, "currency": "NOK", **figures}

    def test_latest_company_accounts_win_over_unsorted_and_group_records(self) -> None:
        # Real Brreg order for 911004925: group years first, the newest company filing last.
        result = self._with_accounts([self._account(2023, "KONSERN", revenue=9e8), self._account(2024, "KONSERN", revenue=9.5e8),
                                      self._account(2023, revenue=1e8), self._account(2024, revenue=1.1e8), self._account(2025, revenue=1.2e8)])
        revenue = claims(result, "financials.revenue")
        self.assertEqual([(c["account_type"], c["reporting_period"]["end"]) for c in revenue], [("SELSKAP", "2025-12-31")])
        self.assertEqual(claims(result, "financials.previous.revenue")[0]["reporting_period"]["end"], "2024-12-31")
        self.assertIn("Latest filed accounts (2025)", result["summary"]["text"])
        self.assertEqual(validate_envelope(result), [])

    def test_group_only_accounts_are_labelled_as_group(self) -> None:
        result = self._with_accounts([self._account(2025, "KONSERN", revenue=5e6)])
        self.assertIn("Latest filed group accounts (2025)", result["summary"]["text"])

    def test_older_figures_are_not_called_latest_when_register_lists_a_newer_filing(self) -> None:
        result = self._with_accounts([self._account(2020, revenue=81200.0)], filed_years=["2019", "2020", "2025"])
        text = result["summary"]["text"]
        self.assertNotIn("Latest filed", text)
        self.assertIn("Most recent accounts with published figures (2020)", text)
        self.assertIn("filed for 2025", text)

    def test_year_on_year_comparison_cites_both_filings(self) -> None:
        result = self._with_accounts([self._account(2024, revenue=1e6, annual_result=-50000.0), self._account(2025, revenue=1.2e6, annual_result=80000.0)])
        point = next(p for p in result["summary"]["points"] if p["topic"] == "comparison")
        self.assertEqual(point["text"], "Compared with 2024, in 2025: revenue up 20% (NOK 1.0m to NOK 1.2m); annual result turned from a loss of NOK 50.0k to a profit of NOK 80.0k.")
        cited = {claim["evidence_ids"][0] for claim in claims(result, "financials.revenue") + claims(result, "financials.previous.revenue")}
        self.assertEqual(set(point["evidence_ids"]), cited)
        self.assertIn("revenue up 20% on the previous year", result["summary"]["points"][0]["text"])

    def test_stale_figures_are_not_called_latest_in_the_takeaway(self) -> None:
        result = self._with_accounts([self._account(2020, revenue=81200.0, operating_result=38200.0, equity=9e5, assets=1e6)], filed_years=["2025"])
        takeaway = result["summary"]["points"][0]["text"]
        self.assertIn("its 2020 accounts (a newer filing is not yet published)", takeaway)
        self.assertNotIn("latest", takeaway)

    def test_two_losses_read_as_narrowed_or_widened(self) -> None:
        result = self._with_accounts([self._account(2024, annual_result=-88900.0, equity=-33700.0), self._account(2025, annual_result=-760.0, equity=-34500.0)])
        point = next(p for p in result["summary"]["points"] if p["topic"] == "comparison")
        self.assertIn("annual result: loss narrowed from NOK 88.9k to NOK 760", point["text"])
        self.assertIn("negative equity widened from NOK 33.7k to NOK 34.5k", point["text"])
        self.assertNotIn("NOK -", point["text"])

    def test_unchanged_and_profit_to_profit_changes_read_with_a_direction(self) -> None:
        result = self._with_accounts([self._account(2024, revenue=0.0, operating_result=-5390.0, annual_result=61613.0),
                                      self._account(2025, revenue=0.0, operating_result=-5390.0, annual_result=408777.0)])
        point = next(p for p in result["summary"]["points"] if p["topic"] == "comparison")
        self.assertEqual(point["text"], "Compared with 2024, in 2025: operating result: loss unchanged at NOK 5.4k; annual result up from NOK 61.6k to NOK 408.8k.")

    def test_operating_profit_with_a_net_loss_is_not_called_profitable(self) -> None:
        result = self._with_accounts([self._account(2025, revenue=459291.0, operating_result=82543.0, annual_result=-44062.0, equity=1.2e6, assets=1.5e6)])
        takeaway = result["summary"]["points"][0]
        self.assertIn("an operating profit but a net loss on its latest accounts, with adequate equity", takeaway["text"])
        self.assertNotIn("profitable", takeaway["text"])
        annual = claims(result, "financials.annual_result")[0]
        self.assertTrue(set(annual["evidence_ids"]) <= set(takeaway["evidence_ids"]))

    def test_no_comparison_without_a_prior_filing(self) -> None:
        result = self._with_accounts([self._account(2025, revenue=1.2e6)])
        self.assertFalse(any(p["topic"] == "comparison" for p in result["summary"]["points"]))


if __name__ == "__main__":
    unittest.main()
