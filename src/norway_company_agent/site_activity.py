"""Dated public activity from the verified company website only: articles on the home page and on
one same-host news/press index page. An item is published only with a machine-readable date
(JSON-LD datePublished or an HTML <time datetime>) and a same-host URL; undated links are skipped."""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

import extruct
from bs4 import BeautifulSoup

from .budget import BudgetExhausted
from .evidence import evidence

NEWS_TERMS = ("nyheter", "aktuelt", "news", "presse", "press", "blogg", "blog", "artikler", "media", "nytt")
ARTICLE_TYPES = {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"}
MAX_ITEMS = 10
MAX_AGE_DAYS = 3 * 365
SOURCE_CLASS = "company_owned_website"


def _same_host(base_url: str, url: str) -> bool:
    base, other = urllib.parse.urlparse(base_url), urllib.parse.urlparse(url)
    strip = lambda host: host.lower().removeprefix("www.")  # noqa: E731
    return other.scheme in {"http", "https"} and strip(other.netloc) == strip(base.netloc)


def news_index_links(base_url: str, html: str, limit: int = 1) -> list[str]:
    """Same-host links whose path or anchor text names a news/press section, shortest path first."""
    found: dict[str, int] = {}
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        if not _same_host(base_url, url):
            continue
        parsed = urllib.parse.urlparse(url)
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        if not any(re.search(r"(?<![a-z])" + term + r"(?![a-z])", haystack) for term in NEWS_TERMS):
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        found[clean] = len([part for part in parsed.path.split("/") if part])
    return [url for url, _ in sorted(found.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _parse_date(value: Any, now: datetime) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        day = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc)
    except ValueError:
        return None
    if day > now + timedelta(days=1) or day < now - timedelta(days=MAX_AGE_DAYS):
        return None
    return day.date().isoformat()


def _jsonld_items(nodes: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    for node in nodes:
        if isinstance(node, list):
            yield from _jsonld_items(node)
        elif isinstance(node, dict):
            if "@graph" in node:
                yield from _jsonld_items(node["@graph"])
            if "itemListElement" in node:
                yield from _jsonld_items(item.get("item", item) if isinstance(item, dict) else item for item in node["itemListElement"])
            kinds = node.get("@type")
            kinds = set(kinds) if isinstance(kinds, list) else {kinds}
            if kinds & ARTICLE_TYPES:
                yield node


def dated_articles(page_url: str, html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Dated same-host articles on one page, from JSON-LD first, then <time datetime> inside a linked block."""
    now = now or datetime.now(timezone.utc)
    items: dict[str, dict[str, Any]] = {}
    try:
        jsonld = extruct.extract(html, base_url=page_url, syntaxes=["json-ld"]).get("json-ld") or []
    except Exception:
        jsonld = []
    for node in _jsonld_items(jsonld):
        date = _parse_date(node.get("datePublished") or node.get("dateCreated"), now)
        url = urllib.parse.urljoin(page_url, str(node.get("url") or node.get("mainEntityOfPage") or page_url)) if not isinstance(node.get("mainEntityOfPage"), dict) else urllib.parse.urljoin(page_url, str(node.get("url") or page_url))
        title = " ".join(str(node.get("headline") or node.get("name") or "").split())[:200]
        if date and title and _same_host(page_url, url):
            items.setdefault(url, {"title": title, "url": url, "date": date, "extraction": "json_ld_date_published", "claim_span": f"{title} — datePublished {node.get('datePublished') or node.get('dateCreated')}"})
    soup = BeautifulSoup(html, "lxml")
    for time_tag in soup.select("time[datetime]"):
        date = _parse_date(time_tag.get("datetime"), now)
        if not date:
            continue
        block = time_tag
        anchor = None
        for _ in range(5):  # nearest enclosing block that holds a link
            block = block.parent
            if block is None:
                break
            anchor = block.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href], a[href]")
            if anchor is not None:
                break
        if anchor is None:
            continue
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or ""))
        heading = block.select_one("h1, h2, h3, h4") if block is not None else None
        title = " ".join((heading or anchor).get_text(" ", strip=True).split())[:200]
        if not title or not _same_host(page_url, url) or url.rstrip("/") == page_url.rstrip("/"):
            continue
        items.setdefault(url, {"title": title, "url": url, "date": date, "extraction": "html_time_datetime", "claim_span": f"{title} — <time datetime=\"{time_tag.get('datetime')}\">"})
    return sorted(items.values(), key=lambda item: item["date"], reverse=True)


def site_activity(
    website: Mapping[str, Any],
    home: Any,
    fetch: Callable[[str], Any] | None,
    robots_allowed: Callable[[str], Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evidence record of dated activity on the verified company website (home page + one news index)."""
    if website.get("status") != "available" or home is None or not getattr(home, "final_url", None):
        return evidence("public_activity", "not_available", SOURCE_CLASS, str(website.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter"),
                        note="No verified company website to read dated activity from")
    pages = [home]
    note = None
    if fetch is not None and robots_allowed is not None:
        for link in news_index_links(home.final_url, home.html):
            try:
                if robots_allowed(link) is not True:
                    note = f"News index {link} not fetched: robots.txt or host unreachable"
                    continue
                page = fetch(link)
            except BudgetExhausted as exc:
                note = f"News index not fetched: {exc}"
                continue
            if page.status == 200 and page.html and page.final_url and _same_host(home.final_url, page.final_url):
                pages.append(page)
    items: dict[str, dict[str, Any]] = {}
    for page in pages:
        for item in dated_articles(page.final_url, page.html, now):
            items.setdefault(item["url"], {**item, "source_url": page.final_url, "retrieved_at": page.retrieved_at, "content_sha256": page.content_sha256})
    ordered = sorted(items.values(), key=lambda item: item["date"], reverse=True)[:MAX_ITEMS]
    if not ordered:
        return evidence("public_activity", "not_available", SOURCE_CLASS, home.final_url, retrieved_at=home.retrieved_at, content_sha256=home.content_sha256,
                        note=note or f"No dated articles on the verified site ({len(pages)} page(s) read)")
    return evidence("public_activity", "available", SOURCE_CLASS, home.final_url, value={"items": ordered},
                    retrieved_at=home.retrieved_at, content_sha256=home.content_sha256, note=note)
