"""Job postings the verified company website itself lists: schema.org JobPosting data on its careers
page, or links from that page to individual ads on known applicant-tracking/job-board hosts. The site
is already tied to the exact entity; ads are company-claimed and keep their own URL."""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

import extruct
from bs4 import BeautifulSoup

from .budget import BudgetExhausted

CAREER_TERMS = ("ledige-stillinger", "ledige stillinger", "stillinger", "karriere", "jobb", "jobs", "careers", "career", "work-with-us", "jobbe-hos-oss", "bli-med")
# Hosts whose URLs identify one job ad (path pattern), linked from the company's own careers page.
ATS_AD_PATTERNS = (
    re.compile(r"^https?://(?:[\w-]+\.)?webcruiter\.(?:no|com)/.*(?:advert|job|stilling|Vacancy)", re.I),
    re.compile(r"^https?://[\w-]+\.teamtailor\.com/jobs/\d+", re.I),
    re.compile(r"^https?://(?:[\w-]+\.)?jobylon\.com/(?:[\w-]+/)?jobs/\d+", re.I),
    re.compile(r"^https?://(?:[\w-]+\.)?reachmee\.com/.*(?:job|vacancy|annonse)", re.I),
    re.compile(r"^https?://(?:www\.)?finn\.no/(?:job/(?:fulltime|parttime|management)/ad\.html\?finnkode=\d+|job/ad/\d+)", re.I),
    re.compile(r"^https?://arbeidsplassen\.nav\.no/stillinger/stilling/[\w-]+", re.I),
    re.compile(r"^https?://(?:[\w-]+\.)?easycruit\.com/vacancy/\d+", re.I),
    re.compile(r"^https?://(?:[\w-]+\.)?recman\.(?:no|io)/job\.php\?job_id=\d+", re.I),
    re.compile(r"^https?://(?:[\w-]+\.)?hr-manager\.net/.*(?:ProjectId|projectId)=\d+", re.I),
)
# Recruitment hosts a company's own "careers"/"ledige stillinger" link may point to.
ATS_HOSTS = re.compile(r"(?:^|\.)(?:webcruiter\.(?:no|com)|teamtailor\.com|jobylon\.com|reachmee\.com|easycruit\.com|recman\.(?:no|io)|"
                       r"hr-manager\.net|jobbnorge\.no|varbi\.com|workday(?:jobs)?\.com|myworkdayjobs\.com|smartrecruiters\.com|"
                       r"successfactors\.(?:eu|com)|talentech\.com|hrmanager\.no)$", re.I)
# Paths that mention a careers term but are not a careers page (news items, privacy notices).
NOT_CAREERS = ("personvern", "privacy", "cookie", "/nyheter/", "/news/", "/artikkel", "/article")
MAX_POSTINGS = 15


def _same_host(base_url: str, url: str) -> bool:
    strip = lambda host: host.lower().removeprefix("www.")  # noqa: E731
    return strip(urllib.parse.urlparse(base_url).netloc) == strip(urllib.parse.urlparse(url).netloc)


def _career_anchors(base_url: str, html: str, same_host: bool) -> dict[str, tuple[int, str]]:
    """{clean url: (term rank, anchor html)} for careers links, same-host or on a careers/ATS host."""
    ranked: dict[str, tuple[int, str]] = {}
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or _same_host(base_url, url) != same_host:
            continue
        if not same_host and not ATS_HOSTS.search(parsed.netloc):
            continue
        haystack = (parsed.netloc + parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(CAREER_TERMS) if term in haystack), None)
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        if rank is None or clean.rstrip("/") == base_url.rstrip("/") or any(term in parsed.path.casefold() for term in NOT_CAREERS):
            continue
        if clean not in ranked or rank < ranked[clean][0]:
            ranked[clean] = (rank, " ".join(str(anchor).split())[:300])
    return ranked


def career_links(base_url: str, html: str, limit: int = 1) -> list[str]:
    """Same-host careers-page links, most specific term first."""
    ranked = _career_anchors(base_url, html, same_host=True)
    return [url for url, _ in sorted(ranked.items(), key=lambda item: (item[1][0], item[0]))[:limit]]


def external_careers_link(base_url: str, html: str) -> tuple[str, str] | None:
    """(url, anchor html) of a link from the verified site to its careers page on a recruitment host."""
    ranked = _career_anchors(base_url, html, same_host=False)
    best = sorted(ranked.items(), key=lambda item: (item[1][0], item[0]))[:1]
    return (best[0][0], best[0][1][1]) if best else None


def _date(value: Any) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else None


def _jobposting_nodes(nodes: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    for node in nodes:
        if isinstance(node, list):
            yield from _jobposting_nodes(node)
        elif isinstance(node, dict):
            if "@graph" in node:
                yield from _jobposting_nodes(node["@graph"])
            if "itemListElement" in node:
                yield from _jobposting_nodes(item.get("item", item) if isinstance(item, dict) else item for item in node["itemListElement"])
            kinds = node.get("@type")
            if "JobPosting" in (kinds if isinstance(kinds, list) else [kinds]):
                yield node


def postings_on_page(page_url: str, html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """JobPosting structured data first, then links to individual ads on known ATS/job-board hosts.
    Expired postings (validThrough in the past) are dropped."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    found: dict[str, dict[str, Any]] = {}
    try:
        jsonld = extruct.extract(html, base_url=page_url, syntaxes=["json-ld"]).get("json-ld") or []
    except Exception:
        jsonld = []
    for node in _jobposting_nodes(jsonld):
        title = " ".join(str(node.get("title") or node.get("name") or "").split())[:200]
        valid = _date(node.get("validThrough"))
        if not title or (valid and valid < today):
            continue
        url = urllib.parse.urljoin(page_url, str(node.get("url") or page_url))
        posted = _date(node.get("datePosted"))
        if posted and posted < (now - timedelta(days=365)).date().isoformat() and not valid:
            continue  # a year-old posting with no end date is stale, not a current hiring signal
        found.setdefault(url + "#" + title, {"title": title, "url": url, "published": posted, "expires": valid,
                                             "extraction": "json_ld_jobposting", "claim_span": f"JobPosting: {title}"})
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip())
        if not any(pattern.match(url) for pattern in ATS_AD_PATTERNS):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())[:200]
        if len(title) < 4 or title.casefold() in {"les mer", "søk", "sok", "apply", "read more", "søk her", "se stillingen"}:
            parent = anchor.find_parent(["li", "article", "div"])
            heading = parent.select_one("h2, h3, h4") if parent is not None else None
            title = " ".join((heading.get_text(" ", strip=True) if heading else "").split())[:200]
        if len(title) < 4:
            continue
        found.setdefault(url, {"title": title, "url": url, "published": None, "expires": None,
                               "extraction": "careers_page_ad_link", "claim_span": f'<a href="{url}">{title}</a>'})
    return list(found.values())[:MAX_POSTINGS]


def site_postings(home: Any, fetch: Callable[[str], Any] | None, robots_allowed: Callable[[str], Any] | None,
                  now: datetime | None = None) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """(postings, note, careers page) from the verified site's home page and its careers page."""
    if home is None or not getattr(home, "final_url", None):
        return [], "No verified company website", None
    pages = [home]
    note = None
    careers: dict[str, Any] | None = None
    links = career_links(home.final_url, home.html)
    anchors = _career_anchors(home.final_url, home.html, same_host=True)
    external = external_careers_link(home.final_url, home.html)
    if external:
        careers = {"url": external[0], "claim_span": external[1], "source_url": home.final_url, "retrieved_at": home.retrieved_at,
                   "content_sha256": home.content_sha256, "extraction": "careers_link_to_recruitment_host"}
    if fetch is not None and robots_allowed is not None:
        for link in links:
            try:
                if robots_allowed(link) is not True:
                    note = f"Careers page {link} not fetched: robots.txt or host unreachable"
                    continue
                page = fetch(link)
            except BudgetExhausted as exc:
                note = f"Careers page not fetched: {exc}"
                continue
            if page.status == 200 and page.html and page.final_url and _same_host(home.final_url, page.final_url):
                pages.append(page)
                if careers is None:
                    careers = {"url": page.final_url, "claim_span": anchors[link][1], "source_url": home.final_url, "retrieved_at": page.retrieved_at,
                               "content_sha256": page.content_sha256, "extraction": "careers_page_on_verified_site"}
    postings: dict[str, dict[str, Any]] = {}
    for page in pages:
        for item in postings_on_page(page.final_url, page.html, now):
            postings.setdefault(item["url"] + "#" + item["title"], {**item, "source_url": page.final_url, "retrieved_at": page.retrieved_at, "content_sha256": page.content_sha256})
    return list(postings.values())[:MAX_POSTINGS], note, careers
