import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent import news_search  # noqa: E402
from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.news_search import name_pattern, names_entity, news_items, news_mentions  # noqa: E402

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
        fetch = lambda url: calls.append(url) or (200, b"")  # noqa: E731
        shared = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 2}, spend=lambda: None, fetch=fetch, now=NOW)
        short = news_mentions("1", "JIA AS", {}, spend=lambda: None, fetch=fetch, now=NOW)
        self.assertEqual((shared["status"], short["status"]), ("not_applicable", "not_applicable"))
        self.assertEqual(calls, [])

    def test_robots_disallow_blocks(self):
        fetch = lambda url: (200, b"User-agent: *\nDisallow: /news/") if url.endswith("robots.txt") else self.fail("searched")  # noqa: E731
        record = news_mentions("1", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=lambda: None, fetch=fetch, now=NOW)
        self.assertEqual(record["status"], "blocked")

    def test_available_record_becomes_valid_dated_news_claims_next_to_site_items(self):
        body = feed(item("Hermetikken Vinbar AS opplever en kraftig økning", "https://www.aftenbladet.no/a/1"))
        fetch = lambda url: (200, b"User-agent: *\nAllow: /news/search") if url.endswith("robots.txt") else (200, body)  # noqa: E731
        spent = []
        record = news_mentions("912345678", "HERMETIKKEN VINBAR AS", {"hermetikken vinbar": 1}, spend=lambda: spent.append(1), fetch=fetch, now=NOW)
        self.assertEqual((record["status"], len(spent)), ("available", 1))
        site = {"field": "public_activity", "status": "not_available", "source_url": "https://hermetikken.no/", "note": "No dated articles"}
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"public_activity": site, "news_mentions": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 1, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "dated_news"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["value"], "https://www.aftenbladet.no/a/1")
        self.assertEqual((claims[0]["availability"], claims[0]["published_at"], claims[0]["signal_type"]), ("available", "2025-07-22", "news_mention"))
        self.assertEqual([p for p in validate_envelope(envelope) if "dated_news" in p], [])
        self.assertIn("in the news", envelope["summary"]["text"])

    def test_no_news_and_no_site_items_is_one_not_available_claim(self):
        record = {"field": "news_mentions", "status": "not_available", "source_url": "https://www.bing.com/news/search", "note": "none"}
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"news_mentions": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 1, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "dated_news"]
        self.assertEqual([claim["availability"] for claim in claims], ["not_available"])


if __name__ == "__main__":
    unittest.main()
