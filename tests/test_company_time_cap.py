import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.budget import BudgetExhausted, RequestBudget  # noqa: E402


class CompanyTimeCapTest(unittest.TestCase):
    def test_cap_applies_to_site_requests_only_and_starts_at_first_site_request(self):
        now = [0.0]
        budget = RequestBudget(100, 3600, clock=lambda: now[0])
        budget.company_seconds = 90
        budget.spend("912345678", "official", essential=True)  # e.g. history prefetch long before the worker
        now[0] = 500.0
        budget.spend("912345678", "site_page")  # clock starts here, not at the official call
        now[0] = 580.0
        budget.spend("912345678", "site_page")
        now[0] = 591.0
        with self.assertRaises(BudgetExhausted):
            budget.spend("912345678", "site_page")
        budget.spend("912345678", "official", essential=True)  # official calls are never capped

    def test_disabled_by_default(self):
        now = [0.0]
        budget = RequestBudget(100, 3600, clock=lambda: now[0])
        budget.spend("912345678", "site_page")
        now[0] = 1000.0
        budget.spend("912345678", "site_page")


if __name__ == "__main__":
    unittest.main()
