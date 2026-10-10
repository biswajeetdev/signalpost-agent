import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import news_search  # noqa: E402
from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.news_search import NewsPrefetcher, name_pattern, names_entity, news_items, news_mentions  # noqa: E402

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def item(title, url, date="Tue, 22 Jul 2025 21:37:00 GMT", source="Aftenbladet"):
    link = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;tid=1&amp;url=" + url.replace(":", "%3a").replace("/", "%2f") + "&amp;mkt=nb-no"
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><News:Source>{source}</News:Source></item>"


def feed(*items):
    return ('<?xml version="1.0"?><rss xmlns:News="https://www.bing.com:443/news/search?q=x&amp;format=rss"><channel>'
            + "".join(items) + "</channel></rss>").encode()


class NewsSearchTest(unittest.TestCase):
    def setUp(self):
        news_search._robots_answer.clear()

    def test_exact_name_not_inside_a_longer_name(self):
        pattern = name_pattern("NORSK MASKIN AS")
        self.assertFalse(names_entity(pattern, "Midt-Norsk Maskin AS er konkurs"))
        self.assertFalse(names_entity(pattern, "Nord Norsk Maskin AS vokser"))
        self.assertFalse(names_entity(pattern, "Kraftig vekst for Vestlandske Norsk Maskin AS"))
        self.assertFalse(names_entity(pattern, "Norsk Maskin ASA kjøper"))
        self.assertTrue(names_entity(pattern, "Inntektene til Norsk Maskin AS stiger"))
        self.assertTrue(names_entity(pattern, "Norsk Maskin AS: Her er 2025-regnskapet"))
        self.assertTrue(names_entity(pattern, "Inntektene til Norsk Maskin AS stiger kraftig"))
        fritid = name_pattern("FRITID AS")
        self.assertFalse(names_entity(fritid, "Inntektene til Natur og Fritid AS krymper for tredje år på rad"))
        self.assertFalse(names_entity(fritid, "Sport & Fritid AS vokser videre"))
        self.assertTrue(names_entity(fritid, "Inntektene til Fritid AS krymper"))
        self.assertFalse(names_entity(name_pattern("FJORDFISK AS"), "FJORDFISK AS"))

    def test_initials_may_be_written_with_dots(self):
        self.assertIsNotNone(name_pattern("J K E DESIGN LEVANGER AS").search("Bratt fall for J. K. E. Design Levanger AS i 2024"))
        self.assertIsNone(name_pattern("SOLIDEZ AS").search("Solidezas lanserer"))

    def test_items_carry_publisher_url_and_date(self):
        body = feed(
            item("Hermetikken Vinbar AS opplever en kraftig økning", "https://www.aftenbladet.no/a/1"),
            item("Hermetikken Vinbar AS opplever en kraftig økning", "https://www.aftenbladet.no/a/2"),
            item("Vinbar i sentrum åpner", "https://www.aftenbladet.no/a/3"),
            item("Hermetikken Vinbar AS i 2019", "https://www.aftenbladet.no/a/4", date="Mon, 01 Jul 2019 10:00:00 GMT"),
        )
        found = news_items(body, "HERMETIKKEN VINBAR AS", NOW)
        self.assertEqual([entry["url"] for entry in found], ["https://www.aftenbladet.no/a/1"])
        self.assertEqual(found[0]["date"], "2025-07-22")
        self.assertEqual(found[0]["publisher"], "Aftenbladet")

    def test_shared_or_short_names_are_not_searched(self):
        calls = []
        fetch = lambda url, spend: calls.append(url) or (200, b"")  # noqa: E731
        shared = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 2}, spend=lambda: None, fetch=fetch, now=NOW)
        short = news_mentions("1", "JIA AS", {}, spend=lambda: None, fetch=fetch, now=NOW)
        self.assertEqual((shared["status"], short["status"]), ("not_applicable", "not_applicable"))
        self.assertEqual(calls, [])

    def test_shared_words_but_unique_exact_as_name_is_searched(self):
        from norway_company_agent.news_search import eligible_name

        shared = {"tannlege bauge": 2}
        self.assertIsNone(eligible_name("TANNLEGE BAUGE AS", shared, {"tannlege bauge as": 1, "tannlege bauge": 1}))
        self.assertIsNotNone(eligible_name("TANNLEGE BAUGE AS", shared, {"tannlege bauge as": 2}))
        self.assertIsNotNone(eligible_name("TANNLEGE BAUGE AS", shared, None))
        self.assertIsNotNone(eligible_name("TANNLEGE BAUGE DA", shared, {"tannlege bauge da": 1}))

    def test_robots_disallow_blocks(self):
        fetch = lambda url, spend: (200, b"User-agent: *\nDisallow: /news/") if url.endswith("robots.txt") else self.fail("searched")  # noqa: E731
        record = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=lambda: None, fetch=fetch, now=NOW)
        self.assertEqual(record["status"], "blocked")

    def test_claims_rest_on_the_publisher_page_not_on_bing(self):
        body = feed(
            item("Hermetikken Vinbar AS opplever en kraftig økning", "https://www.aftenbladet.no/a/1"),
            item("Eksplosiv økning for Hermetikken Vinbar AS", "https://www.aftenbladet.no/a/2"),
            item("Hermetikken Vinbar AS i vekst", "https://www.aftenbladet.no/a/3"),
        )
        pages = {
            # Publisher headline and date differ from Bing's: the publisher's own page is what counts.
            "https://www.aftenbladet.no/a/1": '<html><head><meta property="og:title" content="Hermetikken Vinbar AS opplever en kraftig &#248;kning i inntektene">'
                                              '<meta property="article:published_time" content="2025-07-23T02:37:49Z"></head></html>',
            "https://www.aftenbladet.no/a/2": "<html><head><title>Eksplosiv økning for Bryggen Hermetikken Vinbar AS</title>"
                                              '<script>{"datePublished": "2024-07-31T05:21:00Z"}</script></head></html>',
            "https://www.aftenbladet.no/a/3": "<html><head><title>Hermetikken Vinbar AS i vekst</title></head></html>",  # undated
        }
        spent = []

        def fetch(url, spend):
            spend()
            if url.endswith("robots.txt"):
                return 200, b"User-agent: *\nDisallow: /search"
            return (200, pages[url].encode()) if url in pages else (200, body)

        record = news_mentions("912345678", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=lambda: spent.append(1), fetch=fetch, now=NOW)
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(spent), 6)  # two robots.txt, the search, three article pages: all charged
        [verified] = record["value"]["items"]
        self.assertEqual((verified["url"], verified["date"], verified["publisher"]), ("https://www.aftenbladet.no/a/1", "2025-07-23", "aftenbladet.no"))
        self.assertEqual(verified["title"], "Hermetikken Vinbar AS opplever en kraftig økning i inntektene")
        self.assertEqual(verified["source_url"], "https://www.aftenbladet.no/a/1")
        self.assertEqual(len(verified["content_sha256"]), 64)
        site = {"field": "public_activity", "status": "not_available", "source_url": "https://hermetikken.no/", "note": "No dated articles"}
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"public_activity": site, "news_mentions": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 1, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "dated_news"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"], "https://www.aftenbladet.no/a/1")
        self.assertEqual((claims[0]["availability"], claims[0]["published_at"], claims[0]["signal_type"]), ("available", "2025-07-23", "news_mention"))
        cited = [ev for ev in envelope["evidence"] if ev["id"] in claims[0]["evidence_ids"]]
        self.assertEqual([(ev["source_url"], ev["source_class"]) for ev in cited], [("https://www.aftenbladet.no/a/1", "news_publisher_page")])
        self.assertEqual([p for p in validate_envelope(envelope) if "dated_news" in p], [])
        self.assertIn("in the news", envelope["summary"]["text"])

    def test_publisher_robots_disallow_means_no_claim(self):
        body = feed(item("Hermetikken Vinbar AS opplever en kraftig økning", "https://www.aftenbladet.no/a/1"))

        def fetch(url, spend):
            if url == "https://www.aftenbladet.no/robots.txt":
                return 200, b"User-agent: *\nDisallow: /"
            if url.endswith("robots.txt"):
                return 200, b""
            return (200, body) if "bing.com" in url else self.fail("article fetched")

        record = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=lambda: None, fetch=fetch, now=NOW)
        self.assertEqual(record["status"], "not_available")

    def test_prefetcher_searches_every_company_and_stops_at_the_margin(self):
        from norway_company_agent.budget import RequestBudget

        searched = []

        def fetch(url, spend):
            spend()
            if url.endswith("robots.txt"):
                return 200, b""
            searched.append(url)
            return 200, feed()

        companies = [("1", "HERMETIKKEN VINBAR AS"), ("2", "WYSSEN NORGE AS"), ("3", "JIA AS")]
        budget = RequestBudget(100, 600)
        unshared = type("Unshared", (), {"get": lambda self, key, default=0: 1})()  # like _SharedOnly: absent means unique
        prefetch = NewsPrefetcher(companies, unshared, budget, fetch=fetch, threads=2).start()
        self.assertTrue(prefetch.done.wait(5))
        self.assertEqual({org: prefetch.record(org)["status"] for org, _ in companies}, {"1": "not_available", "2": "not_available", "3": "not_applicable"})
        self.assertEqual(len(searched), 2)
        late = NewsPrefetcher(companies, {}, RequestBudget(100, 30), fetch=fetch, threads=1, margin_seconds=60).start()
        self.assertTrue(late.done.wait(5))
        self.assertEqual(late.results, {})
        self.assertEqual(late.final_record("1")["status"], "failed")
        self.assertIn("Deferred", late.final_record("1")["note"])

    def test_redirect_to_private_address_is_refused_and_hops_are_charged(self):
        spent = []
        handler = news_search._ChargedRedirects(lambda: spent.append(1))
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/admin")
        self.assertEqual(spent, [])

    def test_budget_exhaustion_is_a_failed_record(self):
        from norway_company_agent.budget import BudgetExhausted

        def spend():
            raise BudgetExhausted("run request budget exhausted")
        record = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=spend, fetch=lambda url, charge: charge() or (200, b""), now=NOW)
        self.assertEqual(record["status"], "failed")

    def test_no_news_and_no_site_items_is_one_not_available_claim(self):
        record = {"field": "news_mentions", "status": "not_available", "source_url": "https://www.bing.com/news/search", "note": "none"}
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"news_mentions": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 1, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "dated_news"]
        self.assertEqual([claim["availability"] for claim in claims], ["not_available"])


if __name__ == "__main__":
    unittest.main()
