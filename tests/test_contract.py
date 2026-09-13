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


if __name__ == "__main__":
    unittest.main()
