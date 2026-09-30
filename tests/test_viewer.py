import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.contract import synthesis  # noqa: E402
from norway_company_agent.viewer import render  # noqa: E402


class ViewerTest(unittest.TestCase):
    def test_renders_escaped_card_with_sources_and_summary(self):
        claims = [
            {"field": "legal_name", "value": "ACME <BYGG> AS", "availability": "available", "evidence_ids": ["ev-1"]},
            {"field": "financials.revenue", "value": {"amount": 1200000, "currency": "NOK"}, "availability": "available", "evidence_ids": ["ev-2"], "reporting_period": {"end": "2025-12-31"}},
            {"field": "official_website", "value": None, "availability": "not_available", "evidence_ids": [], "note": "No candidate carried an official tie"},
        ]
        envelope = {
            "organisation_number": "912345678", "legal_name": "ACME <BYGG> AS", "modules": {"registry": "available", "website": "not_available"},
            "claims": claims, "changes": [], "summary": synthesis(claims),
            "evidence": [{"id": "ev-1", "source_url": "https://data.brreg.no/x", "source_class": "official_registry_bulk", "retrieved_at": "2026-09-30T00:00:00Z", "claim_span": "navn: ACME"},
                         {"id": "ev-2", "source_url": "https://data.brreg.no/regnskap", "source_class": "official_annual_accounts", "retrieved_at": "2026-09-30T00:00:00Z"}],
        }
        page = render([envelope], run_id="r1", generated_at="2026-09-30")
        self.assertIn("ACME &lt;BYGG&gt; AS", page)
        self.assertNotIn("<BYGG>", page)
        self.assertIn('href="https://data.brreg.no/regnskap"', page)
        self.assertIn("1,200,000 NOK", page)
        self.assertIn("revenue NOK 1.2m", page)
        self.assertIn("no verified official website", page)
        self.assertIn('name="viewport"', page)


if __name__ == "__main__":
    unittest.main()
