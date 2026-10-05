import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.history_prefetch import HistoryPrefetcher  # noqa: E402
from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.pipeline import finalize_deferred  # noqa: E402

YEARS = [{"aar": 2024}, {"aar": 2025}]


def fetcher_for(calls):
    def make(budget, org):
        def fetch(url):
            calls.append(org)
            return FetchResult(url, 200, 1, 1, YEARS, content_sha256="h", retrieved_at="2026-10-01T00:00:00Z")
        return fetch
    return make


class HistoryPrefetchTest(unittest.TestCase):
    def test_fetches_in_input_order(self):
        calls = []
        prefetch = HistoryPrefetcher(["111111111", "222222222"], RequestBudget(100, 600), fetcher_for(calls), seconds_per_call=0, margin_seconds=0).start()
        self.assertTrue(prefetch.done.wait(5))
        self.assertEqual(calls, ["111111111", "222222222"])
        self.assertEqual(prefetch.final_record("111111111")["status"], "available")

    def test_stops_at_time_margin_and_reports_failure(self):
        calls = []
        prefetch = HistoryPrefetcher(["111111111"], RequestBudget(100, 10), fetcher_for(calls), seconds_per_call=2.1, margin_seconds=60).start()
        self.assertTrue(prefetch.done.wait(5))
        self.assertEqual(calls, [])
        self.assertEqual(prefetch.final_record("111111111")["status"], "failed")

    def test_finalize_fills_deferred_history(self):
        calls = []
        prefetch = HistoryPrefetcher(["912345678"], RequestBudget(100, 600), fetcher_for(calls), seconds_per_call=0, margin_seconds=0)
        profile = {"organisation_number": "912345678", "evidence": {"financial_history": {"field": "financial_history", "status": "failed", "deferred": True,
                   "source_url": "https://x", "source_class": "official_annual_account_copies", "retrieved_at": "t", "note": "Deferred"}}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        prefetch.start()
        counts = finalize_deferred([envelope], [profile], budget=RequestBudget(100, 600), history_prefetch=prefetch)
        self.assertEqual(counts["history"], 1)
        self.assertEqual(profile["evidence"]["financial_history"]["status"], "available")

    def test_refresh_diff_sees_the_filled_history_not_the_placeholder(self):
        import copy
        from norway_company_agent.refresh import diff_profile

        def deferred_profile():
            return {"organisation_number": "912345678", "evidence": {"financial_history": {"field": "financial_history", "status": "failed", "deferred": True,
                    "source_url": "https://x", "source_class": "official_annual_account_copies", "retrieved_at": "t", "note": "Deferred"}}}

        first = deferred_profile()
        prefetch = HistoryPrefetcher(["912345678"], RequestBudget(100, 600), fetcher_for([]), seconds_per_call=0, margin_seconds=0).start()
        finalize_deferred([build_envelope(first, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})], [first],
                          budget=RequestBudget(100, 600), history_prefetch=prefetch)
        previous = {"912345678": copy.deepcopy(first)}
        second = deferred_profile()
        batch_changes = diff_profile(copy.deepcopy(first), second)  # what the batch-time diff records
        self.assertTrue(any(change["field"] == "financial_history.years" for change in batch_changes))
        envelope = build_envelope(second, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0}, changes=batch_changes)
        envelopes = [envelope]
        prefetch = HistoryPrefetcher(["912345678"], RequestBudget(100, 600), fetcher_for([]), seconds_per_call=0, margin_seconds=0).start()
        finalize_deferred(envelopes, [second], budget=RequestBudget(100, 600), history_prefetch=prefetch, previous=previous)
        self.assertEqual([change["field"] for change in envelopes[0]["changes"]], [])


if __name__ == "__main__":
    unittest.main()
