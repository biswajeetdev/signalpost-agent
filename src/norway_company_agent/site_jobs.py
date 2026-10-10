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
from .candidates import registered_domain

# Never the bare singular "stilling": it is inside "bestilling" (an order or booking) on every shop.
# Nor "join us" (events, newsletters) or "rekruttering" (a service recruitment agencies sell, not their own vacancies).
CAREER_TERMS = ("ledige-stillinger", "ledige stillinger", "ledig-stilling", "ledig stilling", "stillinger", "karriere", "jobb", "jobs",
                "careers", "career", "vacancies", "work-with-us", "jobbe-hos-oss", "jobbe hos oss", "jobbmuligheter", "bli-med")
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
    # Patterns below follow the URL shapes catalogued by github.com/kalil0321/ats-scrapers (MIT), rewritten here.
    re.compile(r"^https?://jobs\.lever\.co/[\w.-]+/[0-9a-f-]{36}", re.I),
    re.compile(r"^https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/[\w.-]+/jobs/\d+", re.I),
    re.compile(r"^https?://apply\.workable\.com/[\w.-]+/j/[0-9A-F]+", re.I),
    re.compile(r"^https?://[\w-]+\.recruitee\.com/o/[\w-]+", re.I),
    re.compile(r"^https?://[\w-]+\.jobs\.personio\.(?:de|com)/job/\d+", re.I),
    re.compile(r"^https?://(?:www\.)?jobbnorge\.no/(?:en/)?(?:ledige-stillinger|available-jobs)/stilling/\d+", re.I),
    re.compile(r"^https?://[\w-]+\.varbi\.com/.*(?:job|what:job)", re.I),
    re.compile(r"^https?://[\w-]+\.homerun\.co/[\w-]+", re.I),
    re.compile(r"^https?://jobs\.smartrecruiters\.com/[\w.-]+/\d+", re.I),
    re.compile(r"^https?://[\w-]+\.wd\d+\.myworkdayjobs\.com/.+/job/", re.I),
)
# Recruitment hosts a company's own "careers"/"ledige stillinger" link may point to.
ATS_HOSTS = re.compile(r"(?:^|\.)(?:webcruiter\.(?:no|com)|teamtailor\.com|jobylon\.com|reachmee\.com|easycruit\.com|recman\.(?:no|io)|"
                       r"hr-manager\.net|jobbnorge\.no|varbi\.com|workday(?:jobs)?\.com|myworkdayjobs\.com|smartrecruiters\.com|"
                       r"successfactors\.(?:eu|com)|talentech\.com|hrmanager\.no|lever\.co|greenhouse\.io|workable\.com|"
                       r"recruitee\.com|personio\.(?:de|com)|homerun\.co|bamboohr\.com|jobvite\.com|simployer\.(?:no|com))$", re.I)
# Paths that mention a careers term but are not a careers page (news items, privacy notices).
NOT_CAREERS = ("personvern", "privacy", "cookie", "/nyheter/", "/news/", "/artikkel", "/article")
MAX_POSTINGS = 15
# Button and link text that names the action, not the job: the title is then taken from the nearest heading.
GENERIC_AD_TEXT = frozenset({"les mer", "søk", "sok", "apply", "read more", "søk her", "se stillingen", "send søknad", "søk nå",
                             "søk på stillingen", "søk stillingen", "apply now", "apply here", "view job", "se annonse", "mer info"})


def _same_host(base_url: str, url: str) -> bool:
    """Same site: the same host, or a subdomain of the verified site's registered domain (karriere.firma.no)."""
    strip = lambda host: host.lower().removeprefix("www.")  # noqa: E731
    if strip(urllib.parse.urlparse(base_url).netloc) == strip(urllib.parse.urlparse(url).netloc):
        return True
    domain = registered_domain(base_url)
    return bool(domain) and registered_domain(url) == domain


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


def _microdata_jobpostings(items: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    """schema.org JobPosting items written as microdata, flattened to the JSON-LD field names."""
    for item in items:
        if not isinstance(item, dict):
            continue
        kinds = item.get("type") if isinstance(item.get("type"), list) else [item.get("type")]
        if any(str(kind or "").rstrip("/").endswith("JobPosting") for kind in kinds):
            props = item.get("properties") or {}
            flat = {key: (value[0] if isinstance(value, list) and value else value) for key, value in props.items()}
            yield {**{key: value for key, value in flat.items() if not isinstance(value, dict)}, "_extraction": "microdata_jobposting"}
        for value in (item.get("properties") or {}).values():
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, dict) and "type" in child:
                    yield from _microdata_jobpostings([child])


def postings_on_page(page_url: str, html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """JobPosting structured data first, then links to individual ads on known ATS/job-board hosts.
    Expired postings (validThrough in the past) are dropped."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    found: dict[str, dict[str, Any]] = {}
    try:
        extracted = extruct.extract(html, base_url=page_url, syntaxes=["json-ld", "microdata"])
    except Exception:
        extracted = {}
    nodes = list(_jobposting_nodes(extracted.get("json-ld") or [])) + list(_microdata_jobpostings(extracted.get("microdata") or []))
    for node in nodes:
        title = " ".join(str(node.get("title") or node.get("name") or "").split())[:200]
        valid = _date(node.get("validThrough"))
        if not title or (valid and valid < today):
            continue
        url = urllib.parse.urljoin(page_url, str(node.get("url") or page_url))
        posted = _date(node.get("datePosted"))
        if posted and posted < (now - timedelta(days=365)).date().isoformat() and not valid:
            continue  # a year-old posting with no end date is stale, not a current hiring signal
        found.setdefault(url + "#" + title, {"title": title, "url": url, "published": posted, "expires": valid,
                                             "extraction": node.get("_extraction", "json_ld_jobposting"), "claim_span": f"JobPosting: {title}"})
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip())
        if not any(pattern.match(url) for pattern in ATS_AD_PATTERNS):
            continue
        title = re.sub(r"\s*\((?:opens in (?:a )?new (?:tab|window)|åpnes i ny fane)\)\s*$", "", " ".join(anchor.get_text(" ", strip=True).split()), flags=re.I)[:200]
        if len(title) < 4 or title.casefold() in GENERIC_AD_TEXT:
            parent = anchor.find_parent(["li", "article", "div"])
            heading = parent.select_one("h2, h3, h4") if parent is not None else None
            title = " ".join((heading.get_text(" ", strip=True) if heading else "").split())[:200]
        if len(title) < 4:
            continue
        found.setdefault(url, {"title": title, "url": url, "published": None, "expires": None,
                               "extraction": "careers_page_ad_link", "claim_span": f'<a href="{url}">{title}</a>'})
    return list(found.values())[:MAX_POSTINGS]


def _get(link: str, fetch: Callable[[str], Any], robots_allowed: Callable[[str], Any]) -> tuple[Any, str | None]:
    """(page, note): a robots-checked fetch that never raises on budget exhaustion."""
    try:
        if robots_allowed(link) is not True:
            return None, f"{link} not fetched: robots.txt or host unreachable"
        return fetch(link), None
    except BudgetExhausted as exc:
        return None, f"{link} not fetched: {exc}"


def site_postings(home: Any, fetch: Callable[[str], Any] | None, robots_allowed: Callable[[str], Any] | None,
                  now: datetime | None = None) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """(postings, note, careers page) from the verified site's home page, its careers page (one level deeper
    when that page lists no ads), and the recruitment-host careers page the site itself links to."""
    if home is None or not getattr(home, "final_url", None):
        return [], "No verified company website", None
    pages = [home]
    notes: list[str] = []
    careers: dict[str, Any] | None = None
    links = career_links(home.final_url, home.html)
    anchors = _career_anchors(home.final_url, home.html, same_host=True)
    external = external_careers_link(home.final_url, home.html)
    if external:
        careers = {"url": external[0], "claim_span": external[1], "source_url": home.final_url, "retrieved_at": home.retrieved_at,
                   "content_sha256": home.content_sha256, "extraction": "careers_link_to_recruitment_host"}
    if fetch is not None and robots_allowed is not None:
        for link in links:
            page, note = _get(link, fetch, robots_allowed)
            if note:
                notes.append(f"Careers page {note}")
            if page is not None and page.status == 200 and page.html and page.final_url and _same_host(home.final_url, page.final_url):
                pages.append(page)
                if careers is None:
                    careers = {"url": page.final_url, "claim_span": anchors[link][1], "source_url": home.final_url, "retrieved_at": page.retrieved_at,
                               "content_sha256": page.content_sha256, "extraction": "careers_page_on_verified_site"}
                if not postings_on_page(page.final_url, page.html, now):
                    # A careers landing page often links on to its own "ledige stillinger" list: read one level deeper.
                    deeper = [url for url in career_links(page.final_url, page.html) if url.rstrip("/") != page.final_url.rstrip("/")]
                    for url in deeper[:1]:
                        sub, note = _get(url, fetch, robots_allowed)
                        if note:
                            notes.append(f"Careers sub-page {note}")
                        if sub is not None and sub.status == 200 and sub.html and sub.final_url and _same_host(home.final_url, sub.final_url):
                            pages.append(sub)
        if external:
            # The verified site links here itself, so ads listed on this recruitment page are the company's own.
            ats, note = _get(external[0], fetch, robots_allowed)
            if note:
                notes.append(f"Recruitment page {note}")
            if ats is not None and ats.status == 200 and ats.html and ats.final_url and ATS_HOSTS.search(urllib.parse.urlparse(ats.final_url).netloc):
                pages.append(ats)
    postings: dict[str, dict[str, Any]] = {}
    for page in pages:
        for item in postings_on_page(page.final_url, page.html, now):
            postings.setdefault(item["url"] + "#" + item["title"], {**item, "source_url": page.final_url, "retrieved_at": page.retrieved_at, "content_sha256": page.content_sha256})
    return list(postings.values())[:MAX_POSTINGS], ("; ".join(notes) or None), careers
