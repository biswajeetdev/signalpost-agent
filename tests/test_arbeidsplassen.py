import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import news_search  # noqa: E402
from norway_company_agent.arbeidsplassen import attach_board_ads, board_candidates, employer_on_page, matching_hits  # noqa: E402
from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def hit(uuid, employer, status="ACTIVE", expires="2026-12-10T10:59:20+01:00", business=None):
    return {"_source": {"uuid": uuid, "status": status, "expires": expires, "published": "2026-10-01T09:00:00+02:00",
                        "title": f"Ad {uuid}", "businessName": business or employer, "employer": {"name": employer}}}


def ad_page(orgnr, name):
    # The ad page embeds its data as escaped JSON, as arbeidsplassen.nav.no does.
    return ('<script>self.__next_f.push([1,"{\\"employer\\":{\\"orgnr\\":\\"' + orgnr + '\\",\\"name\\":\\"' + name + '\\",\\"sector\\":\\"Privat\\"}}"])</script>').encode()


LOCATIONS = {"status": "available", "value": {"locations": [{"organisation_number": "973152351", "name": "VMB TOTAL AS AVD BERGEN"}]}}


class JobBoardTest(unittest.TestCase):
    def setUp(self):
        news_search._robots_answer.clear()

    def test_only_active_unexpired_exact_employer_hits(self):
        body = {"hits": {"hits": [
            hit("a", "VMB TOTAL AS"),
            hit("b", "VMB TOTAL AS", status="INACTIVE"),
            hit("c", "VMB TOTAL AS", expires="2026-01-01T00:00:00+01:00"),
            hit("d", "VMB TOTAL BYGG AS"),
            hit("e", "Annen Arbeidsgiver AS", business="Vmb Total AS"),
        ]}}
        self.assertEqual([h["uuid"] for h in matching_hits(body, "VMB TOTAL AS", NOW)], ["a", "e"])

    def test_employer_number_is_read_from_the_ad_page(self):
        self.assertEqual(employer_on_page(ad_page("935228204", "VMB TOTAL AS").decode()), ("935228204", "VMB TOTAL AS"))
        self.assertIsNone(employer_on_page("<html>no employer</html>"))

    def test_candidates_then_attach_matches_entity_and_subunits_only(self):
        search = json.dumps({"hits": {"hits": [hit("a", "VMB TOTAL AS"), hit("b", "VMB TOTAL AS"), hit("c", "VMB TOTAL AS")]}}).encode()
        pages = {"a": ad_page("935228204", "VMB TOTAL AS"), "b": ad_page("973152351", "VMB TOTAL AS AVD BERGEN"), "c": ad_page("999999999", "VMB TOTAL AS")}
        spent = []

        def fetch(url, spend):
            spend()
            if url.endswith("robots.txt"):
                return 200, b"User-agent: *\nAllow: /"
            if "/api/search" in url:
                return 200, search
            return 200, pages[url.rsplit("/", 1)[-1]]

        found = board_candidates("VMB TOTAL AS", spend=lambda: spent.append(1), fetch=fetch, now=NOW)
        self.assertEqual((found["state"], len(found["ads"]), len(spent)), ("ok", 3, 5))  # robots, search, three ad pages
        nav_failed = {"field": "jobs", "status": "failed", "source_url": "https://pam-stilling-feed.nav.no/api/v1/feed", "note": "NAV public feed token unavailable"}
        record = attach_board_ads(nav_failed, found, "935228204", LOCATIONS)
        self.assertEqual(record["status"], "available")
        ads = record["value"]["ads"]
        self.assertEqual([(ad["uuid"], ad["employer_is_subunit"]) for ad in ads], [("a", False), ("b", True)])  # 999999999 rejected
        envelope = build_envelope({"organisation_number": "935228204", "evidence": {"jobs": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 5, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "hiring_signal"]
        self.assertEqual([claim["relation"] for claim in claims], ["exact_employer", "registered_subunit_employer"])
        cited = {ev["source_class"] for ev in envelope["evidence"] if ev["id"] in {i for c in claims for i in c["evidence_ids"]}}
        self.assertEqual(cited, {"nav_job_board_search"})
        self.assertEqual([p for p in validate_envelope(envelope) if "hiring" in p], [])

    def test_merges_with_feed_ads_without_duplicates(self):
        feed = {"field": "jobs", "status": "available", "source_url": "x", "value": {"ads": [{"uuid": "a", "url": "u"}]}, "note": "feed"}
        found = {"state": "ok", "ads": [{"uuid": "a", "employer_organisation_number": "935228204"}, {"uuid": "z", "employer_organisation_number": "935228204"}]}
        merged = attach_board_ads(feed, found, "935228204", None)
        self.assertEqual([ad["uuid"] for ad in merged["value"]["ads"]], ["a", "z"])
        self.assertIsNone(attach_board_ads(merged, {"state": "ok", "ads": []}, "935228204", None))

    def test_rate_limit_stops_the_stream(self):
        from norway_company_agent.arbeidsplassen import BoardPrefetcher
        from norway_company_agent.budget import RequestBudget

        calls = []

        def fetch(url, spend):
            calls.append(url)
            return (200, b"") if url.endswith("robots.txt") else (429, b"")

        board = BoardPrefetcher([("1", "VMB TOTAL AS"), ("2", "ANNET NAVN AS"), ("3", "TREDJE AS")], RequestBudget(100, 600), fetch=fetch, min_interval=0).start()
        self.assertTrue(board.done.wait(5))
        self.assertEqual(board.results, {})
        self.assertIn("429", board.note)
        self.assertEqual(len([url for url in calls if "/api/search" in url]), 1)

    def test_failed_feed_and_empty_board_search_is_not_available(self):
        record = attach_board_ads({"status": "failed", "note": "token"}, {"state": "ok", "ads": [], "note": "0 hits"}, "935228204", None)
        self.assertEqual(record["status"], "not_available")
        self.assertIsNone(attach_board_ads({"status": "failed"}, {"state": "failed", "ads": []}, "935228204", None))


if __name__ == "__main__":
    unittest.main()
