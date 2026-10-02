import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import pipeline  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.guardrails import check_run, enforce_site_basis  # noqa: E402


class FakeCache:
    def shared_domains(self):
        return {}

    def shared_phones(self):
        return {}

    def name_keys(self):
        return {}


REG = {"field": "registry", "status": "available", "source_url": "https://data.brreg.no/x", "source_class": "official_registry_bulk",
       "retrieved_at": "2026-10-02T00:00:00Z", "value": {"navn": "ACME AS"}}


class WatchdogTest(unittest.TestCase):
    def test_stuck_company_gets_failed_envelope_within_chunk_limit(self):
        release = threading.Event()

        def fake_enrich(profile, **kwargs):
            if profile["organisation_number"] == "222222222":
                release.wait(10)  # stuck far past the chunk limit
            return profile, {"requests": 0, "runtime_ms": 1, "third_party_cost_usd": 0}

        profiles = [{"organisation_number": org, "evidence": {"registry": dict(REG)}} for org in ("111111111", "222222222", "333333333")]
        settings = pipeline.RunSettings(workers=3, chunk_seconds=0.5, output_margin_seconds=0)
        started = time.monotonic()
        with mock.patch.object(pipeline, "enrich_company", side_effect=fake_enrich):
            envelopes, enriched, _ = pipeline.run_batch(profiles, cache=FakeCache(), budget=RequestBudget(100, 600), run_id="r", settings=settings)
        release.set()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([e["organisation_number"] for e in envelopes], ["111111111", "222222222", "333333333"])
        stuck = envelopes[1]
        self.assertTrue(stuck["operations"].get("watchdog"))
        self.assertIn("failed", stuck["modules"].values())
        self.assertEqual([p for e in envelopes for p in validate_envelope(e)], [])


class GuardrailTest(unittest.TestCase):
    def _site(self, proofs, domain="acme.com"):
        return {"field": "website", "status": "available", "source_url": f"https://{domain}/", "source_class": "verified_company_website",
                "retrieved_at": "2026-10-02T00:00:00Z", "value": {"final_url": f"https://{domain}/", "registered_domain": domain,
                                                                   "identity_assessment": {"proofs": proofs},
                                                                   "proof_pages": [{"url": f"https://{domain}/kontakt", "retrieved_at": "2026-10-02T00:00:00Z",
                                                                                    "content_sha256": "a" * 64, "claim_spans": {p: f"span {p}" for p in proofs}}]}}

    def test_weak_site_is_flagged_and_withheld(self):
        profile = {"organisation_number": "912345678", "evidence": {"registry": dict(REG), "website": self._site(["legal_name_on_site", "unique_legal_name_domain"])}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        report = check_run([envelope], [profile], ["912345678"])
        self.assertFalse(report["contract_passed"])
        envelopes = [envelope]
        self.assertEqual(enforce_site_basis(envelopes, [profile], RequestBudget(10, 60)), 1)
        self.assertEqual(envelopes[0]["modules"]["website"], "ambiguous")
        self.assertTrue(check_run(envelopes, [profile], ["912345678"])["contract_passed"])

    def test_identifier_backed_site_passes(self):
        profile = {"organisation_number": "912345678", "evidence": {"registry": dict(REG), "website": self._site(["organisation_number"])}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        self.assertTrue(check_run([envelope], [profile], ["912345678"])["contract_passed"])

    def test_count_and_order_failures_are_hard(self):
        profile = {"organisation_number": "912345678", "evidence": {"registry": dict(REG)}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        report = check_run([envelope], [profile], ["912345678", "999999999"])
        self.assertFalse(report["contract_passed"])

    def test_silent_failure_warnings(self):
        profiles = [{"organisation_number": f"91234567{i}", "evidence": {"registry": dict(REG), "financials": {
            "field": "financials", "status": "failed", "note": "run request budget exhausted", "source_url": "u", "retrieved_at": "t"}}} for i in range(5)]
        envelopes = [build_envelope(p, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0}) for p in profiles]
        report = check_run(envelopes, profiles, [p["organisation_number"] for p in profiles])
        self.assertTrue(any("financials" in w for w in report["warnings"]))
        self.assertTrue(any("budget" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
