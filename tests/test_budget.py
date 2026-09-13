import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import BudgetExhausted, RequestBudget, RobotsCache  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RequestBudgetTest(unittest.TestCase):
    def test_discovery_cannot_use_reserve_but_official_calls_can(self) -> None:
        budget = RequestBudget(100, 60, reserve_fraction=0.1)
        for _ in range(90):
            budget.spend("a", "discovery")
        with self.assertRaises(BudgetExhausted):
            budget.spend("a", "discovery")
        for _ in range(10):
            budget.spend("b", "official", essential=True)
        with self.assertRaises(BudgetExhausted):
            budget.spend("b", "official", essential=True)
        self.assertEqual(budget.used, 100)

    def test_refused_request_is_not_counted(self) -> None:
        budget = RequestBudget(10, 60, reserve_fraction=0)
        budget.spend("a", "discovery", allowance=1)
        with self.assertRaises(BudgetExhausted):
            budget.spend("a", "discovery", allowance=1)
        self.assertEqual(budget.used, 1)
        budget.spend("b", "discovery", allowance=1)
        self.assertEqual(budget.report()["per_company_max"], 1)

    def test_deadline_stops_requests(self) -> None:
        clock = FakeClock()
        budget = RequestBudget(10, 5, clock=clock)
        budget.spend("a", "official", essential=True)
        clock.now = 5.0
        with self.assertRaises(BudgetExhausted):
            budget.spend("a", "official", essential=True)

    def test_concurrent_spend_never_exceeds_limit(self) -> None:
        budget = RequestBudget(500, 60, reserve_fraction=0)

        def worker() -> None:
            for _ in range(200):
                try:
                    budget.spend("x", "discovery")
                except BudgetExhausted:
                    return

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(budget.used, 500)


class RobotsCacheTest(unittest.TestCase):
    def test_loader_runs_once_per_key(self) -> None:
        cache = RobotsCache()
        calls = []
        for _ in range(3):
            cache.get("https://example.no", lambda: calls.append(1) or "parser")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
