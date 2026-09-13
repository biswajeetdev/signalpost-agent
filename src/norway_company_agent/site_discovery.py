from __future__ import annotations

import hashlib
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from bs4 import BeautifulSoup

from .budget import BudgetExhausted, RequestBudget, RobotsCache
from .candidates import SHARED_DOMAIN_THRESHOLD, registered_domain, website_candidates
from .evidence import utc_now
from .proof import STRONG_PROOFS, assess_site_identity, page_proof_spans, registry_identifiers
from .website import USER_AGENT, assert_public_url

MAX_PAGE_BYTES = 1_500_000
MAX_REDIRECTS = 5
CONTACT_TERMS = ("kontakt", "contact", "om-oss", "om_oss", "about")
REGISTRY_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
METHOD = "registry_candidates_with_site_proof_v1"


@dataclass(frozen=True)
class Page:
    requested_url: str
    final_url: str | None
    status: int
    html: str = ""
    content_sha256: str | None = None
    retrieved_at: str = ""
    error: str | None = None


Fetch = Callable[[str], Page]
RobotsCheck = Callable[[str], bool]


def dns_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except (OSError, UnicodeError):
        return False


class _BudgetedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, on_hop: Callable[[], None]) -> None:
        super().__init__()
        self._on_hop = on_hop

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        self._on_hop()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_site_fetchers(
    budget: RequestBudget,
    company: str,
    *,
    allowance: int,
    robots: RobotsCache,
    timeout: float = 8.0,
) -> tuple[Fetch, RobotsCheck]:
    """Fetchers that charge every attempt and redirect hop to this company's allowance."""

    def spend(purpose: str) -> None:
        budget.spend(company, purpose, allowance=allowance)

    def open_url(url: str, accept: str) -> Any:
        assert_public_url(url)
        spend("site_discovery")
        opener = urllib.request.build_opener(_BudgetedRedirectHandler(lambda: spend("site_discovery_redirect")))
        return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept}), timeout=timeout)

    def fetch(url: str) -> Page:
        retrieved_at = utc_now()
        try:
            with open_url(url, "text/html,application/xhtml+xml") as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
                final_url = response.geturl()
                if "html" not in response.headers.get("content-type", "").lower():
                    return Page(url, final_url, response.status, retrieved_at=retrieved_at, error="non-HTML response")
                html = raw[:MAX_PAGE_BYTES].decode("utf-8", errors="replace")
                return Page(url, final_url, response.status, html, hashlib.sha256(raw).hexdigest(), retrieved_at)
        except BudgetExhausted:
            raise
        except urllib.error.HTTPError as exc:
            return Page(url, None, exc.code, retrieved_at=retrieved_at, error=f"HTTP {exc.code}")
        except Exception as exc:  # DNS, TLS, timeout, or a redirect refused by the URL guard
            return Page(url, None, 0, retrieved_at=retrieved_at, error=f"{type(exc).__name__}: {str(exc)[:120]}")

    def load_robots(origin: str) -> urllib.robotparser.RobotFileParser:
        parser = urllib.robotparser.RobotFileParser()
        try:
            with open_url(origin + "/robots.txt", "text/plain") as response:
                parser.parse(response.read(500_000).decode("utf-8", errors="replace").splitlines())
        except BudgetExhausted:
            raise
        except urllib.error.HTTPError as exc:
            # Same convention as urllib.robotparser.read(): auth errors disallow, other 4xx/5xx allow.
            if exc.code in {401, 403}:
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except Exception:
            parser.allow_all = True
        return parser

    def robots_allowed(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = robots.get(origin, lambda: load_robots(origin))
        return parser.can_fetch(USER_AGENT, url)  # type: ignore[attr-defined]

    return fetch, robots_allowed


def contact_links(base_url: str, html: str, limit: int = 2) -> list[str]:
    """Same-host contact/about links, contact first."""
    base = urllib.parse.urlparse(base_url)
    ranked: dict[str, int] = {}
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(CONTACT_TERMS) if term in haystack), None)
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if rank is None or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        ranked[clean] = min(rank, ranked.get(clean, rank))
    return [url for url, _ in sorted(ranked.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _page_record(page: Page, spans: dict[str, str]) -> dict[str, Any]:
    return {
        "url": page.final_url,
        "requested_url": page.requested_url,
        "http_status": page.status,
        "retrieved_at": page.retrieved_at,
        "content_sha256": page.content_sha256,
        "proofs": sorted(spans),
        "claim_spans": spans,
    }


def _record(status: str, *, source_url: str, retrieved_at: str, attempts: list[dict[str, Any]], value: Any = None, content_sha256: str | None = None, note: str | None = None) -> dict[str, Any]:
    source_class = "verified_company_website" if status == "available" else "website_candidate_search"
    return {
        "field": "website",
        "status": status,
        "source_type": source_class,
        "source_class": source_class,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_sha256": content_sha256,
        "value": value,
        "note": note,
        "method": METHOD,
        "attempts": attempts,
    }


def discover_website(
    row: Mapping[str, Any],
    *,
    shared_domains: Mapping[str, int],
    shared_phones: Mapping[str, int],
    fetch: Fetch,
    robots_allowed: RobotsCheck,
    resolver: Callable[[str], bool] = dns_resolves,
    max_hosts: int = 4,
) -> tuple[dict[str, Any], Page | None]:
    """Find the entity's own website. Returns the evidence record and the verified homepage for reuse.

    States: available (registry identifier on the site), ambiguous (reachable but only an
    administrator/group site), not_available (no candidate proved), failed (budget exhausted).
    """
    identifiers = registry_identifiers(row)
    attempts: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    fallback: dict[str, Any] | None = None
    hosts_fetched = 0
    try:
        for candidate in website_candidates(row, shared_domains):  # type: ignore[arg-type]
            domain = candidate["domain"]
            if hosts_fetched >= max_hosts:
                break
            if domain in seen_domains:
                continue
            host = next((name for name in (domain, "www." + domain) if resolver(name)), None)
            if not host:
                attempts.append({**candidate, "outcome": "no_dns"})
                continue
            base_url = f"https://{host}/"
            if not robots_allowed(base_url):
                attempts.append({**candidate, "outcome": "blocked_robots", "url": base_url})
                continue
            hosts_fetched += 1
            home = fetch(base_url)
            if home.error or not home.final_url:
                attempts.append({**candidate, "outcome": "fetch_failed", "url": base_url, "http_status": home.status, "error": home.error})
                continue
            final_domain = registered_domain(home.final_url)
            seen_domains.update({domain, final_domain})
            relation = candidate["relation"]
            if shared_domains.get(final_domain, 0) >= SHARED_DOMAIN_THRESHOLD:
                relation = "administrator_or_group"
            home_spans = page_proof_spans(identifiers, home.html, shared_phones=shared_phones)
            found = set(home_spans)
            proof_pages = [_page_record(home, home_spans)]
            if not found & STRONG_PROOFS:
                for link in contact_links(home.final_url, home.html):
                    if not robots_allowed(link):
                        continue
                    page = fetch(link)
                    if page.error or registered_domain(page.final_url) != final_domain:
                        continue
                    spans = page_proof_spans(identifiers, page.html, shared_phones=shared_phones)
                    proof_pages.append(_page_record(page, spans))
                    found |= set(spans)
                    if found & STRONG_PROOFS:
                        break
            assessment = assess_site_identity(found, relation)
            attempts.append({**candidate, "outcome": assessment["status"], "url": home.final_url, "final_domain": final_domain, "proofs": assessment["proofs"]})
            value = {
                "final_url": home.final_url,
                "registered_domain": final_domain,
                "candidate_source": candidate["source"],
                "identity_assessment": assessment,
                "proof_pages": proof_pages,
            }
            if assessment["publishable"]:
                return _record("available", source_url=home.final_url, retrieved_at=home.retrieved_at, attempts=attempts, value=value, content_sha256=home.content_sha256), home
            if assessment["status"] in {"related", "weak"} and fallback is None:
                fallback = _record("ambiguous", source_url=home.final_url, retrieved_at=home.retrieved_at, attempts=attempts, value=value, content_sha256=home.content_sha256, note="Reachable site is not proven to be this exact entity; not published")
    except BudgetExhausted as exc:
        return _record("failed", source_url=REGISTRY_SOURCE, retrieved_at=utc_now(), attempts=attempts, note=str(exc)), None
    if fallback:
        return fallback, None
    return _record("not_available", source_url=REGISTRY_SOURCE, retrieved_at=utc_now(), attempts=attempts, note="No candidate website carried an official identifier for this entity"), None
