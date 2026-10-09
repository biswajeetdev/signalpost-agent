"""Dated news about the entity from Bing News RSS (allowed by bing.com/robots.txt; no key). An item is
published only when its headline carries the full legal name, legal form included, not glued to a
preceding word or hyphen ("Midt-Norsk Maskin AS" is not "Norsk Maskin AS"), and only for a legal name
that is distinctive and belongs to no other registry entity. One request per company."""
from __future__ import annotations

import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

from .budget import BudgetExhausted
from .evidence import evidence, utc_now
from .http import read_bounded
from .proof import name_key
from .website import USER_AGENT, assert_public_url

SOURCE_CLASS = "independent_news_discovery"
SEARCH_URL = "https://www.bing.com/news/search?format=rss&setlang=nb&cc=NO&q={query}"
ROBOTS_URL = "https://www.bing.com/robots.txt"
MAX_ITEMS = 10
MAX_AGE_DAYS = 3 * 365
MAX_FEED_BYTES = 500_000
MIN_NAME_CHARS = 5
MIN_INTERVAL_SECONDS = 0.25  # run-wide pacing for the search endpoint
MIN_HEADLINE_WORDS = 2  # words besides the legal name
CONNECTORS = frozenset({"og", "&", "and", "i"})  # words that join parts of a name

Fetch = Callable[[str], tuple[int, bytes]]

_robots_lock = threading.Lock()
_robots_answer: dict[str, bool] = {}
_pace_lock = threading.Lock()
_last_request = [0.0]


def name_pattern(legal_name: str) -> re.Pattern[str] | None:
    """Every word of the legal name in order, separated by spaces or punctuation (single letters may also
    be written together: "J. K. E." or "JKE"), with no word character or hyphen on either side."""
    words = re.findall(r"[0-9a-zæøåäöüé]+", legal_name.casefold())
    if not words:
        return None
    parts = [re.escape(words[0])]
    for previous, word in zip(words, words[1:]):
        separator = r"[\s.,&'/-]*" if len(previous) == 1 and len(word) == 1 else r"[\s.,&'/-]+"
        parts += [separator, re.escape(word)]
    return re.compile(r"(?<![\w-])" + "".join(parts) + r"(?![\w-])", re.I)


def names_entity(pattern: re.Pattern[str], title: str) -> bool:
    """The legal name in a headline as a name of its own. Norwegian headlines are in sentence case, so a
    capitalised word right before it is most likely part of a longer name ("Nord Norsk Maskin AS"), as is
    a capitalised word joined by a connector ("Natur og Fritid AS"). A headline that is only the name, or
    little more, is a listing rather than an article."""
    for match in pattern.finditer(title):
        before = re.findall(r"[^\W\d_]+|&", title[: match.start()])
        rest = re.findall(r"[^\W_]+", title[: match.start()] + " " + title[match.end():])
        if len(rest) < MIN_HEADLINE_WORDS:
            continue
        if before and before[-1][0].isupper():
            continue
        if len(before) >= 2 and before[-1].casefold() in CONNECTORS and before[-2][0].isupper():
            continue
        return True
    return False


def article_url(link: str) -> str | None:
    """The publisher URL behind a Bing News click-through link (its `url` parameter)."""
    parsed = urllib.parse.urlparse(link or "")
    target = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0] if parsed.netloc.endswith("bing.com") else link
    article = urllib.parse.urlparse(target)
    host = (article.hostname or "").lower()
    # Never fetched, only cited: a public-looking web host is enough (no IP literals or local names).
    if article.scheme not in {"http", "https"} or "." not in host or host.endswith((".local", ".localhost")) or re.fullmatch(r"[\d.:\[\]]+", host):
        return None
    return target


def _date(text: str | None, now: datetime) -> str | None:
    try:
        moment = parsedate_to_datetime(text or "")
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment > now + timedelta(days=1) or moment < now - timedelta(days=MAX_AGE_DAYS):
        return None
    return moment.astimezone(timezone.utc).date().isoformat()


def news_items(feed: bytes, legal_name: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Feed items whose headline carries the exact legal name, newest first, one per article URL."""
    now = now or datetime.now(timezone.utc)
    pattern = name_pattern(legal_name)
    if pattern is None:
        return []
    items: dict[str, dict[str, Any]] = {}
    for node in ET.fromstring(feed).iter("item"):
        title = " ".join((node.findtext("title") or "").split())[:200]
        url = article_url(node.findtext("link") or "")
        date = _date(node.findtext("pubDate"), now)
        if not (url and date and names_entity(pattern, title)):
            continue
        publisher = next((child.text for child in node if child.tag.endswith("Source") and child.text), None)
        items.setdefault(url, {
            "title": title,
            "url": url,
            "date": date,
            "publisher": publisher,
            "claim_span": f"{title} — pubDate {node.findtext('pubDate')}" + (f" — {publisher}" if publisher else ""),
        })
    seen_titles: set[str] = set()
    unique = []
    for item in sorted(items.values(), key=lambda item: item["date"], reverse=True):
        if item["title"].casefold() not in seen_titles:
            seen_titles.add(item["title"].casefold())
            unique.append(item)
    return unique[:MAX_ITEMS]


def eligible_name(legal_name: str, name_keys: Mapping[str, int] | Any) -> str | None:
    """Why this legal name cannot be searched safely, or None when it can."""
    key = name_key(legal_name)
    if len(key.replace(" ", "")) < MIN_NAME_CHARS:
        return "Legal name too short to identify the entity in a headline"
    if name_keys.get(key, 0) != 1:
        return "Legal name is shared with another registry entity"
    return None


def _robots_allowed(url: str, fetch: Fetch) -> bool:
    with _robots_lock:
        if "answer" not in _robots_answer:
            parser = urllib.robotparser.RobotFileParser()
            try:
                status, body = fetch(ROBOTS_URL)
                parser.parse(body.decode("utf-8", errors="replace").splitlines()) if status == 200 else None
                _robots_answer["answer"] = status == 200 and parser.can_fetch(USER_AGENT, url.split("&q=")[0] + "&q=x")
            except Exception:
                _robots_answer["answer"] = False
        return _robots_answer["answer"]


def default_fetch(url: str) -> tuple[int, bytes]:
    assert_public_url(url)
    with _pace_lock:
        wait = _last_request[0] + MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/xml,text/xml"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, read_bounded(response, MAX_FEED_BYTES)


def news_mentions(
    org: str,
    legal_name: str,
    name_keys: Mapping[str, int] | Any,
    *,
    spend: Callable[[], None],
    fetch: Fetch = default_fetch,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evidence record of news headlines that name this exact entity."""
    query = urllib.parse.quote(f'"{legal_name}"')
    url = SEARCH_URL.format(query=query)
    if reason := eligible_name(legal_name, name_keys):
        return evidence("news_mentions", "not_applicable", SOURCE_CLASS, url, note=reason)
    if not _robots_allowed(url, fetch):
        return evidence("news_mentions", "blocked", SOURCE_CLASS, url, note="News search not permitted by robots.txt or unreachable")
    retrieved_at = utc_now()
    try:
        spend()
        status, body = fetch(url)
        if status != 200:
            return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=f"HTTP {status}")
        items = news_items(body, legal_name, now)
    except BudgetExhausted as exc:
        return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=str(exc))
    except Exception as exc:  # network error, timeout or malformed feed: no claim either way
        return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=f"{type(exc).__name__}: {str(exc)[:120]}")
    digest = hashlib.sha256(body).hexdigest()
    if not items:
        return evidence("news_mentions", "not_available", SOURCE_CLASS, url, retrieved_at=retrieved_at, content_sha256=digest,
                        note="No news headline carries the exact legal name")
    return evidence("news_mentions", "available", SOURCE_CLASS, url, value={"items": items}, retrieved_at=retrieved_at, content_sha256=digest)
