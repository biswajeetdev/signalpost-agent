"""Dated news about the entity. Bing News RSS (allowed by bing.com/robots.txt; no key) only points to
candidate articles; each is then read on its publisher's own page (robots.txt honoured), and the claim
rests on that page alone: its headline, its machine-readable publication date, its content hash.
An article counts only when the headline carries the full legal name, legal form included, as a name
of its own ("Midt-Norsk Maskin AS" and "Natur og Fritid AS" do not name "Norsk Maskin AS" or
"Fritid AS"), and only for a legal name that is distinctive and unique in the registry. One search
request per company, plus at most three article pages when the search names it."""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import threading
import time
import urllib.error
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
from .site_activity import _parse_date
from .website import USER_AGENT, assert_public_url

SOURCE_CLASS = "independent_news_discovery"
SEARCH_URL = "https://www.bing.com/news/search?format=rss&setlang=nb&cc=NO&q={query}"
ROBOTS_URL = "https://www.bing.com/robots.txt"
MAX_ITEMS = 10
MAX_AGE_DAYS = 3 * 365
MAX_BYTES = 1_500_000
MAX_VERIFIED = 3  # articles read on the publisher's page per company
MIN_NAME_CHARS = 5
MIN_INTERVAL_SECONDS = 0.25  # run-wide pacing for the search endpoint
MIN_HEADLINE_WORDS = 2  # words besides the legal name
CONNECTORS = frozenset({"og", "&", "and", "i"})  # words that join parts of a name

Fetch = Callable[[str, Callable[[], None]], tuple[int, bytes]]  # (url, spend) -> (status, body)

_robots_lock = threading.Lock()
_robots_answer: dict[str, Any] = {}  # origin -> parsed robots.txt, or a blanket allow/deny
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


def _robots_allowed(url: str, fetch: Fetch, spend: Callable[[], None]) -> bool:
    """robots.txt read once per origin per run (charged like any request); a network failure is not
    cached, so a later company tries again, and until then the page is treated as not permitted."""
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with _robots_lock:
        if origin not in _robots_answer:
            try:
                status, body = fetch(origin + "/robots.txt", spend)
            except BudgetExhausted:
                raise
            except Exception:
                return False
            parser = urllib.robotparser.RobotFileParser()
            if status == 200:
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
            # Same convention as site discovery: 401/403 disallow, other 4xx/5xx allow.
            _robots_answer[origin] = parser if status == 200 else status not in {401, 403}
        answer = _robots_answer[origin]
    return answer.can_fetch(USER_AGENT, url) if isinstance(answer, urllib.robotparser.RobotFileParser) else bool(answer)


class _ChargedRedirects(urllib.request.HTTPRedirectHandler):
    """Every redirect hop passes the public-URL guard and is charged to the budget."""

    def __init__(self, spend: Callable[[], None]) -> None:
        super().__init__()
        self._spend = spend

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        self._spend()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url: str, spend: Callable[[], None]) -> tuple[int, bytes]:
    """One guarded, paced, charged request (HTTP errors come back as their status)."""
    assert_public_url(url)
    spend()
    with _pace_lock:
        wait = _last_request[0] + MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/rss+xml,application/xml,text/xml,text/plain"})
    try:
        with urllib.request.build_opener(_ChargedRedirects(spend)).open(request, timeout=10) as response:
            return response.status, read_bounded(response, MAX_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _meta(html: str, key: str) -> str | None:
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        if re.search(r"""(?:property|name)\s*=\s*["']""" + re.escape(key) + r"""["']""", tag, re.I):
            found = re.search(r"""content\s*=\s*["']([^"']*)["']""", tag, re.I)
            if found:
                return html_lib.unescape(found.group(1)).strip()
    return None


def article_facts(html: str, now: datetime) -> tuple[str | None, str | None, str | None]:
    """(headline, publication date, the date text as printed) from the publisher's own article page."""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    headline = _meta(html, "og:title") or (html_lib.unescape(" ".join(title.group(1).split())) if title else None)
    for raw in (_meta(html, "article:published_time"), *re.findall(r'"datePublished"\s*:\s*"([^"]+)"', html)[:1]):
        if raw and (date := _parse_date(raw, now)):
            return headline, date, raw
    return headline, None, None


def verify_on_publisher(item: Mapping[str, Any], pattern: re.Pattern[str], fetch: Fetch, spend: Callable[[], None], now: datetime) -> dict[str, Any] | None:
    """The article as the publisher states it: its own headline names the entity and it carries a
    machine-readable publication date. Bing only pointed to it; nothing from Bing is kept."""
    url = str(item["url"])
    if not _robots_allowed(url, fetch, spend):
        return None
    retrieved_at = utc_now()
    status, body = fetch(url, spend)
    if status != 200 or not body:
        return None
    html = body.decode("utf-8", errors="replace")
    headline, date, raw_date = article_facts(html, now)
    if not headline or not date or not names_entity(pattern, " ".join(headline.split())[:200]):
        return None
    headline = " ".join(headline.split())[:200]
    return {
        "title": headline,
        "url": url,
        "date": date,
        "publisher": urllib.parse.urlparse(url).netloc.lower().removeprefix("www."),
        "source_url": url,
        "retrieved_at": retrieved_at,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "claim_span": f"{headline} — published {raw_date}",
    }


def news_mentions(
    org: str,
    legal_name: str,
    name_keys: Mapping[str, int] | Any,
    *,
    spend: Callable[[], None],
    fetch: Fetch = default_fetch,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evidence record of news articles that name this exact entity, each read on its publisher's page."""
    now = now or datetime.now(timezone.utc)
    query = urllib.parse.quote(f'"{legal_name}"')
    url = SEARCH_URL.format(query=query)
    if reason := eligible_name(legal_name, name_keys):
        return evidence("news_mentions", "not_applicable", SOURCE_CLASS, url, note=reason)
    retrieved_at = utc_now()
    verified: list[dict[str, Any]] = []
    note = None
    try:
        if not _robots_allowed(url, fetch, spend):
            return evidence("news_mentions", "blocked", SOURCE_CLASS, url, note="News search not permitted by robots.txt or unreachable")
        status, body = fetch(url, spend)
        if status != 200:
            return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=f"HTTP {status}")
        candidates = news_items(body, legal_name, now)
        pattern = name_pattern(legal_name)
        for item in candidates[:MAX_VERIFIED]:
            try:
                if found := verify_on_publisher(item, pattern, fetch, spend, now):
                    verified.append(found)
            except BudgetExhausted:
                raise
            except Exception as exc:  # one unreadable article never sinks the others
                note = f"An article could not be read: {type(exc).__name__}"
    except BudgetExhausted as exc:
        if not verified:
            return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=str(exc))
        note = str(exc)
    except Exception as exc:  # network error, timeout or malformed feed: no claim either way
        return evidence("news_mentions", "failed", SOURCE_CLASS, url, retrieved_at=retrieved_at, note=f"{type(exc).__name__}: {str(exc)[:120]}")
    if not verified:
        return evidence("news_mentions", "not_available", SOURCE_CLASS, url, retrieved_at=retrieved_at,
                        note=note or "No news article names the exact legal name on its publisher's page")
    return evidence("news_mentions", "available", SOURCE_CLASS, url, value={"items": verified}, retrieved_at=retrieved_at, note=note)
