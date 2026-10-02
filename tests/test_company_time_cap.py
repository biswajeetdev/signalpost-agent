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
        budget.spend("912345678", "site_page", allowance=32)  # clock starts here, not at the official call
        now[0] = 580.0
        budget.spend("912345678", "site_page", allowance=32)
        now[0] = 591.0
        with self.assertRaises(BudgetExhausted):
            budget.spend("912345678", "site_page", allowance=32)
        budget.spend("912345678", "official", essential=True)  # official calls are never capped

    def test_run_level_feed_reader_is_never_time_capped(self):
        # Regression: the NAV feed reader spends as '_run' for ~18 minutes; the cap killed it after 90 s.
        now = [0.0]
        budget = RequestBudget(1000, 3600, clock=lambda: now[0])
        budget.company_seconds = 90
        for minute in range(20):
            now[0] = minute * 60.0
            budget.spend("_run", "jobs_feed")
            budget.spend("912345678", "jobs_detail")

    def test_disabled_by_default(self):
        now = [0.0]
        budget = RequestBudget(100, 3600, clock=lambda: now[0])
        budget.spend("912345678", "site_page")
        now[0] = 1000.0
        budget.spend("912345678", "site_page")


if __name__ == "__main__":
    unittest.main()
