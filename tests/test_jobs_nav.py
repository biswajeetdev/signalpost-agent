import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.jobs_nav import FEED_ROOT, FEED_URL, NavJobIndex, company_jobs, employer_homepages  # noqa: E402


def feed_item(uuid, business, status="ACTIVE"):
    return {"id": uuid, "url": f"/api/v1/feedentry/{uuid}", "_feed_entry": {"uuid": uuid, "status": status, "businessName": business, "title": "Role " + uuid, "sistEndret": "2026-09-29T10:00:00+02:00"}}


def detail(uuid, orgnr, homepage=None):
    return {"uuid": uuid, "status": "ACTIVE", "ad_content": {
        "title": "Role " + uuid, "employer": {"name": "X", "orgnr": orgnr, "homepage": homepage},
        "published": "2026-09-26T00:00:00+02:00", "expires": "2026-10-26T00:00:00+02:00",
        "workLocations": [{"city": "OSLO", "municipal": "OSLO"}], "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
    }}


class FakeFeed:
    def __init__(self, pages, details):
        self.pages, self.details, self.calls = pages, details, []

    def __call__(self, url, attempts=2, headers=None, on_attempt=None, timeout=20.0):
        self.calls.append(url)
        if on_attempt:
            on_attempt()
        if url == FEED_URL or url.startswith(FEED_ROOT + "/api/v1/feed/"):
            index = 0 if url == FEED_URL else int(url.rsplit("/", 1)[-1])
            body = {"items": self.pages[index], "next_url": f"/api/v1/feed/{index + 1}" if index + 1 < len(self.pages) else None}
            return FetchResult(url, 200, 1, 1, body, content_sha256="p", retrieved_at="2026-09-30T00:00:00Z")
        uuid = url.rsplit("/", 1)[-1]
        if uuid in self.details:
            return FetchResult(url, 200, 1, 1, self.details[uuid], content_sha256="d" + uuid, retrieved_at="2026-09-30T00:00:00Z")
        return FetchResult(url, 404, 1, 0, error="HTTP 404", retrieved_at="2026-09-30T00:00:00Z")


def built(pages, details):
    fake = FakeFeed(pages, details)
    return NavJobIndex(token="t", fetcher=fake).build(), fake


class NavJobsTest(unittest.TestCase):
    def test_publishes_only_when_employer_orgnr_matches(self):
        index, _ = built(
            [[feed_item("a", "Acme Bygg AS"), feed_item("b", "ACME BYGG AS")], [feed_item("c", "Acme Bygg", status="INACTIVE")]],
            {"a": detail("a", "912345678", "https://acmebygg.no"), "b": detail("b", "999999999")},
        )
        self.assertEqual(index.pages, 2)
        record = company_jobs("912345678", "ACME BYGG AS", None, index)
        self.assertEqual(record["status"], "available")
        self.assertEqual([ad["uuid"] for ad in record["value"]["ads"]], ["a"])
        self.assertIn("1 name-matched ads rejected", record["note"])
        self.assertEqual(employer_homepages(record), ["https://acmebygg.no"])

    def test_subunit_employer_counts_and_is_labelled(self):
        index, _ = built([[feed_item("s", "Acme Kafe")]], {"s": detail("s", "811111111")})
        locations = {"status": "available", "value": {"locations": [{"organisation_number": "811111111", "name": "ACME KAFE"}]}}
        record = company_jobs("912345678", "ACME HOLDING AS", locations, index)
        self.assertEqual(record["status"], "available")
        ad = record["value"]["ads"][0]
        self.assertTrue(ad["employer_is_subunit"])
        self.assertEqual(employer_homepages(record), [])

    def test_no_match_is_not_available_and_failed_index_is_failed(self):
        index, _ = built([[feed_item("a", "Other AS")]], {})
        self.assertEqual(company_jobs("912345678", "ACME BYGG AS", None, index)["status"], "not_available")
        broken = NavJobIndex(token="t", fetcher=lambda *a, **k: FetchResult(FEED_URL, 0, 0, 0, error="URLError", retrieved_at="x")).build()
        self.assertEqual(broken.state, "failed")
        self.assertEqual(company_jobs("912345678", "ACME BYGG AS", None, broken)["status"], "failed")

    def test_envelope_claims_validate(self):
        index, _ = built([[feed_item("a", "Acme Bygg AS")]], {"a": detail("a", "912345678")})
        profile = {"organisation_number": "912345678", "evidence": {"jobs": company_jobs("912345678", "ACME BYGG AS", None, index)}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        jobs = [claim for claim in envelope["claims"] if claim["field"] == "hiring_signal"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["relation"], "exact_employer")
        self.assertEqual(jobs[0]["signal_type"], "job_posting")
        self.assertIsInstance(jobs[0]["value"], str)
        self.assertEqual(envelope["modules"]["jobs"], "available")
        self.assertEqual([p for p in validate_envelope(envelope) if "hiring_signal" in p], [])


class BoundedReadTest(unittest.TestCase):
    def test_trickling_body_is_abandoned(self):
        from norway_company_agent.http import SlowResponse, read_bounded

        class Trickle:
            def read(self, size):
                import time
                time.sleep(0.02)
                return b"x"

        with self.assertRaises(SlowResponse):
            read_bounded(Trickle(), 10_000, max_seconds=0.1)

    def test_normal_body_and_limit(self):
        import io

        from norway_company_agent.http import read_bounded
        self.assertEqual(read_bounded(io.BytesIO(b"abcdef"), 4), b"abcd")
        self.assertEqual(read_bounded(io.BytesIO(b"abc"), 100), b"abc")


class DeferredFillTest(unittest.TestCase):
    def test_deferred_jobs_are_filled_after_the_batch(self):
        from norway_company_agent.budget import RequestBudget
        from norway_company_agent.pipeline import fill_deferred_jobs

        index = NavJobIndex(token="t", fetcher=FakeFeed([[feed_item("a", "Acme Bygg AS")]], {"a": detail("a", "912345678")}))
        waiting = company_jobs("912345678", "ACME BYGG AS", None, index, wait_seconds=0.0)
        self.assertTrue(waiting["deferred"])
        profile = {"organisation_number": "912345678", "name": "ACME BYGG AS",
                   "evidence": {"registry": {"status": "available", "value": {"navn": "ACME BYGG AS"}}, "jobs": waiting}}
        envelope = build_envelope(profile, run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        self.assertEqual(envelope["modules"]["jobs"], "failed")
        index.build()
        envelopes, profiles = [envelope], [profile]
        self.assertEqual(fill_deferred_jobs(envelopes, profiles, jobs_index=index, budget=RequestBudget(100, 600)), 1)
        self.assertEqual(envelopes[0]["modules"]["jobs"], "available")
        self.assertEqual(envelopes[0]["run"]["run_id"], "r")
        self.assertEqual([p for p in validate_envelope(envelopes[0]) if "job" in p], [])


if __name__ == "__main__":
    unittest.main()
