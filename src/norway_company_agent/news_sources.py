"""Deeper dated-news sources on the verified company website, used when the home page and news index
carry no machine-readable dates: the site's own RSS/Atom feed, or individual article pages (found on
the index page or in the site's sitemap) whose own markup states the publication date. Every item is
same-host and dated by the article or feed itself; listing order or sitemap lastmod never dates it."""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

import extruct
from bs4 import BeautifulSoup

from .budget import BudgetExhausted

MAX_ARTICLE_FETCHES = 5  # article pages read at most; stop after MAX_ARTICLES dated ones
MAX_ARTICLES = 3
MAX_FEED_ITEMS = 10
MAX_AGE_DAYS = 3 * 365
# A path segment that names a news section, followed by at least one more segment (the article).
NEWS_SECTION = re.compile(r"/(?:[\w-]*-)?(nyheter|nyhet|aktuelt|news|newsroom|presse|press|pressemeldinger|blogg|blog|artikler|artikkel|"
                          r"articles?|nytt|innlegg|posts?)(?:-[\w-]*)?/[^/?#]+", re.I)
ARTICLE_TYPES = {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report", "WebPage"}
DATE_META = ("article:published_time", "datepublished", "date", "pubdate", "publish_date", "publishdate", "dc.date", "dc.date.issued", "dcterms.created")


def _same_host(base_url: str, url: str) -> bool:
    strip = lambda host: host.lower().removeprefix("www.")  # noqa: E731
    other = urllib.parse.urlparse(url)
    return other.scheme in {"http", "https"} and strip(other.netloc) == strip(urllib.parse.urlparse(base_url).netloc)


def _day(value: Any, now: datetime) -> str | None:
    text = str(value or "").strip()
    day = None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    try:
        if match:
            day = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
        elif text:
            parsed = parsedate_to_datetime(text)  # RFC 822 dates in RSS
            day = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None
    if day is None or day > now + timedelta(days=1) or day < now - timedelta(days=MAX_AGE_DAYS):
        return None
    return day.date().isoformat()


def _get(url: str, fetch: Callable[[str], Any], robots_allowed: Callable[[str], Any]) -> Any:
    try:
        if robots_allowed(url) is not True:
            return None
        page = fetch(url)
    except BudgetExhausted:
        return None
    return page if page.status == 200 and page.html and page.final_url and _same_host(url, page.final_url) else None


def feed_links(base_url: str, html: str) -> list[str]:
    """Same-host RSS/Atom feeds the page advertises with <link rel="alternate">, comment feeds excluded."""
    found = []
    for node in BeautifulSoup(html, "lxml").select('link[rel~="alternate"][href]'):
        kind = str(node.get("type") or "").lower()
        url = urllib.parse.urljoin(base_url, str(node.get("href")))
        if ("rss" in kind or "atom" in kind) and _same_host(base_url, url) and "comment" not in url.lower() and url not in found:
            found.append(url)
    return found


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def feed_items(page: Any, site_url: str, now: datetime) -> list[dict[str, Any]]:
    """Dated same-host entries of one RSS 2.0 or Atom feed."""
    try:
        root = ET.fromstring(page.html.encode("utf-8"))
    except ET.ParseError:
        return []
    items = []
    for entry in (node for node in root.iter() if _local(node.tag) in {"item", "entry"}):
        fields = {_local(child.tag): child for child in entry}
        title = " ".join((fields["title"].text or "").split())[:200] if "title" in fields else ""
        link = fields.get("link")
        url = ((link.text or "").strip() or str(link.get("href") or "")) if link is not None else ""
        raw = next((fields[key].text for key in ("pubdate", "published", "date", "updated") if key in fields and fields[key].text), None)
        date = _day(raw, now)
        if title and date and url and _same_host(site_url, url):
            items.append({"title": title, "url": url, "date": date, "extraction": "rss_atom_feed",
                          "claim_span": f"{title} — {raw.strip()}", "source_url": page.final_url,
                          "retrieved_at": page.retrieved_at, "content_sha256": page.content_sha256})
    return sorted(items, key=lambda item: item["date"], reverse=True)[:MAX_FEED_ITEMS]


def article_links(page_url: str, html: str) -> list[str]:
    """Same-host links that look like individual articles under a news section, in page order."""
    found: list[str] = []
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip()).split("#", 1)[0]
        path = urllib.parse.urlparse(url).path
        if _same_host(page_url, url) and NEWS_SECTION.search(path) and url.rstrip("/") != page_url.rstrip("/") and url not in found:
            found.append(url)
    return found


def sitemap_articles(site_url: str, fetch: Callable[[str], Any], robots_allowed: Callable[[str], Any]) -> list[str]:
    """News-section URLs from the site's sitemap (one level of sitemap index), newest lastmod first.
    lastmod only orders which articles to read; it is never used as the publication date."""
    parsed = urllib.parse.urlparse(site_url)
    page = _get(f"{parsed.scheme}://{parsed.netloc}/sitemap.xml", fetch, robots_allowed)
    entries = _sitemap_entries(page)
    children = [loc for loc, _, is_index in entries if is_index]
    if children:
        preferred = sorted(children, key=lambda loc: (not re.search(r"post|news|nyhet|artik|blog|aktuelt|article", loc, re.I), loc))
        entries = _sitemap_entries(_get(preferred[0], fetch, robots_allowed))
    urls = [(loc, lastmod) for loc, lastmod, is_index in entries if not is_index and _same_host(site_url, loc) and NEWS_SECTION.search(urllib.parse.urlparse(loc).path)]
    return [loc for loc, _ in sorted(urls, key=lambda item: item[1] or "", reverse=True)]


def _sitemap_entries(page: Any) -> list[tuple[str, str | None, bool]]:
    if page is None:
        return []
    try:
        root = ET.fromstring(page.html.encode("utf-8"))
    except ET.ParseError:
        return []
    is_index = _local(root.tag) == "sitemapindex"
    entries = []
    for node in root:
        fields = {_local(child.tag): (child.text or "").strip() for child in node}
        if fields.get("loc"):
            entries.append((fields["loc"], fields.get("lastmod"), is_index))
    return entries


def _jsonld_nodes(nodes: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        if isinstance(node, list):
            yield from _jsonld_nodes(node)
        elif isinstance(node, dict):
            if "@graph" in node:
                yield from _jsonld_nodes(node["@graph"])
            yield node


def article_item(page: Any, now: datetime) -> dict[str, Any] | None:
    """The article page's own publication date (JSON-LD datePublished, then date meta tags) and title."""
    soup = BeautifulSoup(page.html, "lxml")
    raw = title = None
    try:
        jsonld = extruct.extract(page.html, base_url=page.final_url, syntaxes=["json-ld"]).get("json-ld") or []
    except Exception:
        jsonld = []
    for node in _jsonld_nodes(jsonld):
        kinds = node.get("@type")
        kinds = set(kinds) if isinstance(kinds, list) else {kinds}
        if kinds & ARTICLE_TYPES and node.get("datePublished"):
            raw, title = node["datePublished"], node.get("headline") or node.get("name")
            break
    source = "json_ld_date_published"
    if raw is None:
        for meta in soup.select("meta[content]"):
            key = str(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
            if key in DATE_META:
                raw, source = meta["content"], f"meta_{key}"
                break
    date = _day(raw, now)
    if not date:
        return None
    og = soup.select_one('meta[property="og:title"][content]')
    heading = soup.select_one("h1")
    title = " ".join(str(title or (og["content"] if og else "") or (heading.get_text(" ", strip=True) if heading else "")).split())[:200]
    if not title:
        return None
    return {"title": title, "url": page.final_url, "date": date, "extraction": source, "claim_span": f"{title} — datePublished {raw}",
            "source_url": page.final_url, "retrieved_at": page.retrieved_at, "content_sha256": page.content_sha256}


def deeper_articles(home: Any, pages: list[Any], fetch: Callable[[str], Any], robots_allowed: Callable[[str], Any],
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Dated items from the site's feed, else from article pages (at most MAX_ARTICLE_FETCHES read)."""
    now = now or datetime.now(timezone.utc)
    for feed_url in feed_links(home.final_url, home.html)[:1]:
        page = _get(feed_url, fetch, robots_allowed)
        items = feed_items(page, home.final_url, now) if page is not None else []
        if items:
            return items
    candidates: list[str] = []
    for page in pages:
        candidates += [url for url in article_links(page.final_url, page.html) if url not in candidates]
    if not candidates:
        candidates = sitemap_articles(home.final_url, fetch, robots_allowed)
    found = []
    for url in candidates[:MAX_ARTICLE_FETCHES]:
        page = _get(url, fetch, robots_allowed)
        item = article_item(page, now) if page is not None else None
        if item:
            found.append(item)
            if len(found) >= MAX_ARTICLES:
                break
    return found
