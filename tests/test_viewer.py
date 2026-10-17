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

    def test_cards_carry_filter_sort_and_source_linked_comparison_data(self):
        import html as html_lib
        import json
        import re

        def money(field, amount, ev, end="2025-12-31"):
            return {"field": field, "value": {"amount": amount, "currency": "NOK"}, "availability": "available", "evidence_ids": [ev], "reporting_period": {"end": end}}

        claims = [
            {"field": "legal_name", "value": "ACME AS", "availability": "available", "evidence_ids": ["ev-1"]},
            {"field": "business_address", "value": {"poststed": "BERGEN"}, "availability": "available", "evidence_ids": ["ev-1"]},
            money("financials.revenue", 1200000, "ev-2"), money("financials.previous.revenue", 1000000, "ev-3", "2024-12-31"),
            {"field": "hiring_signal", "value": "https://acme.no/jobb", "availability": "available", "evidence_ids": ["ev-4"]},
        ]
        evidence = [{"id": "ev-1", "source_url": "https://data.brreg.no/x", "retrieved_at": "t"},
                    {"id": "ev-2", "source_url": "https://data.brreg.no/regnskap/2025", "retrieved_at": "t"},
                    {"id": "ev-3", "source_url": "https://data.brreg.no/regnskap/2024", "retrieved_at": "t"},
                    {"id": "ev-4", "source_url": "https://acme.no/jobb", "retrieved_at": "t"}]
        envelope = {"organisation_number": "912345678", "legal_name": "ACME AS", "modules": {}, "claims": claims, "changes": [],
                    "summary": synthesis(claims), "evidence": evidence}
        page = render([envelope, {**envelope, "organisation_number": "912345679"}], run_id="r1", generated_at="2026-10-17")
        self.assertIn('data-rev="1200000"', page)
        self.assertIn('data-growth="20.0"', page)
        self.assertIn('data-place="Bergen"', page)
        self.assertIn('data-hiring="1"', page)
        self.assertIn('data-web="0"', page)
        self.assertIn('<option value="Bergen">Bergen</option>', page)
        self.assertIn('id="cmp" hidden', page)
        self.assertIn("sort();apply();", page, "cards open in the order the sort box shows")
        self.assertIn(".length>4)x.checked=false", page, "a fifth compare tick is refused")
        rows = {row[0]: row for row in json.loads(html_lib.unescape(re.search(r'data-cmp="([^"]*)"', page).group(1)))["rows"]}
        self.assertEqual(rows["Revenue (2025)"], ["Revenue (2025)", "1,200,000 NOK", "https://data.brreg.no/regnskap/2025"])
        self.assertEqual(rows["Revenue change on previous year"][1:], ["+20.0%", "https://data.brreg.no/regnskap/2024"])
        self.assertEqual(rows["Hiring"][1:], ["yes", "https://acme.no/jobb"])

    def test_summary_flags_negative_equity_and_cites_the_figures(self):
        def money(field, amount, ev):
            return {"field": field, "value": {"amount": amount, "currency": "NOK"}, "availability": "available", "evidence_ids": [ev], "reporting_period": {"end": "2025-12-31"}}

        claims = [money("financials.revenue", 1000000, "ev-r"), money("financials.operating_result", -200000, "ev-o"),
                  money("financials.equity", -50000, "ev-e"), money("financials.assets", 500000, "ev-a")]
        summary = synthesis(claims)
        health = next(point for point in summary["points"] if point["topic"] == "financial_health")
        self.assertIn("operating margin -20%", health["text"])
        self.assertIn("negative equity", health["text"])
        self.assertEqual(health["evidence_ids"], ["ev-a", "ev-e", "ev-o", "ev-r"])
        self.assertEqual(summary["points"][0]["topic"], "takeaway")
        self.assertIn("financially at risk (operating loss, negative equity)", summary["points"][0]["text"])


if __name__ == "__main__":
    unittest.main()
