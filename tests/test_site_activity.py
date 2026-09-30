import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.contract import build_envelope, validate_envelope  # noqa: E402
from norway_company_agent.site_activity import dated_articles, news_index_links, site_activity  # noqa: E402
from norway_company_agent.site_discovery import Page  # noqa: E402

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
HOME = """<html><body>
<nav><a href="/nyheter">Nyheter</a> <a href="/kontakt">Kontakt</a> <a href="https://other.no/news">Partner news</a></nav>
<article><h2><a href="/nyheter/ny-avdeling">Vi åpner ny avdeling i Bergen</a></h2><time datetime="2026-09-12">12. sep</time></article>
<article><h2><a href="https://other.no/x">Foreign post</a></h2><time datetime="2026-09-10">10. sep</time></article>
<article><h2><a href="/nyheter/fremtid">Future</a></h2><time datetime="2027-01-01">1. jan</time></article>
<article><h2><a href="/nyheter/udatert">Undated</a></h2></article>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle","headline":"Årsrapport 2025 er klar","datePublished":"2026-04-20T08:00:00+02:00","url":"https://acme.no/nyheter/arsrapport"}</script>
</body></html>"""


def page(url, html):
    return Page(url, url, 200, html, "a" * 64, "2026-09-30T00:00:00Z")


class SiteActivityTest(unittest.TestCase):
    def test_dated_same_host_articles_only(self):
        items = dated_articles("https://acme.no/", HOME, NOW)
        self.assertEqual([item["url"] for item in items], ["https://acme.no/nyheter/ny-avdeling", "https://acme.no/nyheter/arsrapport"])
        self.assertEqual(items[1]["extraction"], "json_ld_date_published")
        self.assertEqual(items[0]["date"], "2026-09-12")

    def test_news_index_link_is_same_host(self):
        self.assertEqual(news_index_links("https://acme.no/", HOME), ["https://acme.no/nyheter"])

    def test_reads_index_page_and_builds_valid_claims(self):
        index_html = '<ul><li><a href="/nyheter/kontrakt">Ny kontrakt med kommunen</a> <time datetime="2026-08-01">1. aug</time></li></ul>'
        fetched = []

        def fetch(url):
            fetched.append(url)
            return page(url, index_html)

        record = site_activity({"status": "available"}, page("https://acme.no/", HOME), fetch, lambda url: True, NOW)
        self.assertEqual(fetched, ["https://acme.no/nyheter"])
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["items"]), 3)
        envelope = build_envelope({"organisation_number": "912345678", "evidence": {"public_activity": record}},
                                  run_id="r", started_at="s", completed_at="c", operations={"requests": 0, "runtime_ms": 0})
        claims = [claim for claim in envelope["claims"] if claim["field"] == "public_activity"]
        self.assertEqual(len(claims), 3)
        self.assertEqual([p for p in validate_envelope(envelope) if "public_activity" in p], [])

    def test_no_verified_site_is_not_available(self):
        record = site_activity({"status": "ambiguous"}, None, None, None, NOW)
        self.assertEqual(record["status"], "not_available")

    def test_robots_disallow_skips_index(self):
        record = site_activity({"status": "available"}, page("https://acme.no/", HOME), lambda url: self.fail("fetched"), lambda url: False, NOW)
        self.assertEqual(record["status"], "available")
        self.assertIn("robots.txt", record["note"])


if __name__ == "__main__":
    unittest.main()
