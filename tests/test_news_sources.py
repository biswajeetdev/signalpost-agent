import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.news_sources import article_item, deeper_articles, feed_items, feed_links, sitemap_articles  # noqa: E402
from norway_company_agent.site_activity import news_index_links, site_activity  # noqa: E402
from norway_company_agent.site_discovery import Page, discover_website  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acme</title>
<item><title>Ny fabrikk i Moss</title><link>https://acme.no/nyheter/ny-fabrikk</link><pubDate>Tue, 22 Sep 2026 08:00:00 +0200</pubDate></item>
<item><title>Udatert</title><link>https://acme.no/nyheter/udatert</link></item>
<item><title>Ekstern</title><link>https://other.no/x</link><pubDate>Tue, 22 Sep 2026 08:00:00 +0200</pubDate></item>
</channel></rss>"""
SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://acme.no/page-sitemap.xml</loc></sitemap><sitemap><loc>https://acme.no/post-sitemap.xml</loc></sitemap></sitemapindex>"""
POST_SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://acme.no/nyheter/gammel</loc><lastmod>2025-01-01</lastmod></url>
<url><loc>https://acme.no/nyheter/ny</loc><lastmod>2026-09-20</lastmod></url>
<url><loc>https://acme.no/om-oss</loc><lastmod>2026-09-30</lastmod></url></urlset>"""
ARTICLE = """<html><head><meta property="og:title" content="Acme åpner ny fabrikk">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"Acme åpner ny fabrikk","datePublished":"2026-09-22T20:00:00+02:00"}</script>
</head><body><h1>Acme åpner ny fabrikk</h1></body></html>"""


def page(url, html):
    return Page(url, url, 200, html, "a" * 64, "2026-10-01T00:00:00Z")


class NewsSourcesTest(unittest.TestCase):
    def test_feed_items_are_dated_and_same_host(self):
        home = '<link rel="alternate" type="application/rss+xml" href="/feed/"><link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
        self.assertEqual(feed_links("https://acme.no/", home), ["https://acme.no/feed/"])
        items = feed_items(page("https://acme.no/feed/", FEED), "https://acme.no/", NOW)
        self.assertEqual([(item["url"], item["date"]) for item in items], [("https://acme.no/nyheter/ny-fabrikk", "2026-09-22")])

    def test_sitemap_index_prefers_post_sitemap_and_orders_by_lastmod(self):
        pages = {"https://acme.no/sitemap.xml": SITEMAP_INDEX, "https://acme.no/post-sitemap.xml": POST_SITEMAP}
        urls = sitemap_articles("https://acme.no/", lambda url: page(url, pages.get(url, "")), lambda url: True)
        self.assertEqual(urls, ["https://acme.no/nyheter/ny", "https://acme.no/nyheter/gammel"])

    def test_article_page_dates_itself(self):
        item = article_item(page("https://acme.no/nyheter/ny", ARTICLE), NOW)
        self.assertEqual((item["title"], item["date"]), ("Acme åpner ny fabrikk", "2026-09-22"))
        self.assertIsNone(article_item(page("https://acme.no/nyheter/udatert", "<h1>Udatert</h1>"), NOW))

    def test_undated_listing_falls_back_to_article_pages(self):
        home = page("https://acme.no/", '<a href="/nyheter">Nyheter</a>')
        pages = {"https://acme.no/nyheter": '<a href="/nyheter/ny">Acme åpner ny fabrikk</a>', "https://acme.no/nyheter/ny": ARTICLE}
        fetch = lambda url: page(url, pages.get(url, ""))  # noqa: E731
        self.assertEqual(len(deeper_articles(home, [home, fetch("https://acme.no/nyheter")], fetch, lambda url: True, NOW)), 1)
        record = site_activity({"status": "available"}, home, fetch, lambda url: True, NOW)
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"]["items"][0]["url"], "https://acme.no/nyheter/ny")

    def test_news_term_outranks_media(self):
        html = '<a href="/media/">Media</a><a href="/om-oss/nyheter/">Nyheter</a>'
        self.assertEqual(news_index_links("https://acme.no/", html), ["https://acme.no/om-oss/nyheter/"])


class GroupDomainTest(unittest.TestCase):
    ROW = {"organisasjonsnummer": "912345678", "navn": "ACME ASA", "hjemmeside": "www.acme.com", "epostadresse": "", "telefon": "", "mobil": ""}

    def discover(self, row, html):
        return discover_website(row, shared_domains={"acme.com": 9}, shared_phones={}, fetch=lambda url: page(url, html),
                                robots_allowed=lambda url: True, resolver=lambda host: host.endswith("acme.com"))[0]

    def test_registry_declared_group_domain_named_after_the_entity_publishes(self):
        self.assertEqual(self.discover(self.ROW, "<footer>© Acme ASA</footer>")["status"], "available")

    def test_subsidiary_declaring_the_group_domain_does_not(self):
        row = {**self.ROW, "navn": "ACME SERVICES AS"}
        self.assertNotEqual(self.discover(row, "<footer>© Acme ASA · Acme Services AS</footer>")["status"], "available")


if __name__ == "__main__":
    unittest.main()
