import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import chunked  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.contract import build_envelope  # noqa: E402

REG = {"field": "registry", "status": "available", "source_url": "https://data.brreg.no/x", "source_class": "official_registry_bulk",
       "retrieved_at": "2026-10-02T00:00:00Z", "value": {"navn": "ACME AS"}}


def failed(note):
    return {"status": "source_error", "note": note, "source_url": "https://data.brreg.no/x", "retrieved_at": "t"}


def profile(org, note=None):
    records = {"registry": dict(REG)}
    for module in chunked.OFFICIAL_LOOKUPS:
        records[module] = failed(note) if note else {"status": "available", "value": {}, "source_url": "u", "retrieved_at": "t"}
    return {"organisation_number": org, "evidence": records}


class OutageBreakerTest(unittest.TestCase):
    def test_outage_share_counts_network_errors_only(self):
        rows = [profile("1", "URLError"), profile("2", "HTTP 503"), profile("3"), profile("4", "run request budget exhausted")]
        self.assertEqual(chunked.outage_share(rows), 0.5)

    def test_wait_for_source_polls_until_reachable(self):
        answers = iter([False, False, True])
        slept = []
        self.assertTrue(chunked.wait_for_source(RequestBudget(10, 600), max_wait=60, probe=lambda: next(answers), sleep=slept.append))
        self.assertEqual(slept, [15.0, 15.0])

    def test_chunk_is_rerun_after_an_outage(self):
        calls = []

        def fake_run_batch(profiles, **kwargs):
            calls.append(1)
            note = "URLError" if len(calls) == 1 else None  # first attempt during the outage
            enriched = [profile(p["organisation_number"], note) for p in profiles]
            envelopes = [build_envelope(p, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0}) for p in enriched]
            return envelopes, enriched, {}

        profiles = [{"organisation_number": org, "evidence": {"registry": dict(REG)}} for org in ("111111111", "222222222")]

        class Cache:
            snapshots = {}

        with mock.patch.object(chunked, "run_batch", side_effect=fake_run_batch), \
             mock.patch.object(chunked, "wait_for_source", return_value=True) as waited:
            envelopes, enriched, report = chunked.run_chunked(
                profiles, cache=Cache(), budget=RequestBudget(100, 3600), run_id="r", settings=chunked.RunSettings(),
                previous={}, registry_sha256="x", checkpoint_dir=Path(self._tmp()), chunk_size=10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(waited.call_count, 1)
        self.assertEqual(report["chunks"]["outage_retries"], 1)
        self.assertEqual(enriched[0]["evidence"]["financials"]["status"], "available")

    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
