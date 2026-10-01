from __future__ import annotations

import hashlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from bs4 import BeautifulSoup

from .budget import BudgetExhausted, RequestBudget, RobotsCache
from .candidates import SHARED_DOMAIN_THRESHOLD, full_name_labels, registered_domain, website_candidates
from .evidence import utc_now
from .http import read_bounded
from .proof import STRONG_PROOFS, address_span, assess_site_identity, legal_name_span, name_key, norway_span, page_proof_spans, registry_identifiers
from .website import USER_AGENT, assert_public_url

MAX_PAGE_BYTES = 1_500_000
MAX_REDIRECTS = 5
CONTACT_TERMS = ("kontakt", "contact", "om-oss", "om_oss", "about")
# Norwegian sites usually print "Org.nr" on legal pages; read them after contact/about pages.
LEGAL_TERMS = ("personvern", "privacy", "vilkar", "vilkår", "salgsbetingelser", "kjopsbetingelser", "kjøpsbetingelser",
               "betingelser", "terms", "cookie", "impressum", "juridisk")
PROOF_PAGE_LIMIT = 3
REGISTRY_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
METHOD = "registry_candidates_with_site_proof_v3"


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
# True: allowed, False: disallowed by robots.txt, None: host unreachable (skip without a page fetch).
RobotsCheck = Callable[[str], "bool | None"]


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

    def open_url(url: str, accept: str, purpose: str) -> Any:
        assert_public_url(url)
        budget.spend(company, purpose, allowance=allowance)
        hop = lambda: budget.spend(company, purpose + "_redirect", allowance=allowance)  # noqa: E731
        opener = urllib.request.build_opener(_BudgetedRedirectHandler(hop))
        return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept}), timeout=timeout)

    def fetch(url: str) -> Page:
        """One retry for a transient failure (connection error, timeout, 5xx), charged like any request.
        DNS failures, URL-guard refusals and slow-trickle bodies are not retried."""
        page = fetch_once(url)
        transient = page.status >= 500 or (page.status == 0 and page.error and not page.error.startswith(("URLError: <urlopen error [Errno 8]", "SlowResponse", "ValueError", "UnsafeURL")))
        return fetch_once(url) if transient else page

    def fetch_once(url: str) -> Page:
        retrieved_at = utc_now()
        try:
            with open_url(url, "text/html,application/xhtml+xml", "site_page") as response:
                raw = read_bounded(response, MAX_PAGE_BYTES + 1)
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
            with open_url(origin + "/robots.txt", "text/plain", "robots") as response:
                parser.parse(read_bounded(response, 500_000, 10.0).decode("utf-8", errors="replace").splitlines())
        except BudgetExhausted:
            raise
        except urllib.error.HTTPError as exc:
            # Same convention as urllib.robotparser.read(): auth errors disallow, other 4xx/5xx allow.
            if exc.code in {401, 403}:
                parser.disallow_all = True
            else:
                parser.allow_all = True
        except Exception as exc:
            # Connection, TLS or timeout failure: the host cannot serve pages either.
            parser.unreachable = True  # type: ignore[attr-defined]
            parser.unreachable_reason = f"{type(exc).__name__}: {exc}"  # type: ignore[attr-defined]
        return parser

    def _parser(url: str) -> Any:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return robots.get(origin, lambda: load_robots(origin))

    def robots_allowed(url: str) -> bool | None:
        parser = _parser(url)
        if getattr(parser, "unreachable", False):
            return None
        return parser.can_fetch(USER_AGENT, url)  # type: ignore[attr-defined]

    # Why an origin was unreachable, so discovery can skip fallbacks that cannot help (see _fallback_kind).
    robots_allowed.unreachable_reason = lambda url: str(getattr(_parser(url), "unreachable_reason", ""))  # type: ignore[attr-defined]
    return fetch, robots_allowed


PARKED_PATTERNS = re.compile(
    r"(is parked|domain (name )?is for sale|this domain (is|may be) for sale|buy this domain|future home of|"
    r"is registered,? but|domain has (just )?been registered|parkingcrew|sedoparking|bodis\.com|dan\.com|"
    r"domenet er (til salgs|parkert|registrert)|kjøp (dette )?domenet|dette domenet|domene til salgs|"
    r"under construction|kommer snart|coming soon|website is (currently )?under)",
    re.I,
)
MIN_UNIQUE_NAME_CHARS = 5
PARKED_MAX_TEXT_CHARS = 3000


def is_parked(html: str) -> bool:
    """Registrar parking, for-sale and placeholder pages: never evidence for any company."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", html or "")
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    # Parking pages are short; a live site that merely says "coming soon" somewhere is not parked.
    return len(text) < PARKED_MAX_TEXT_CHARS and bool(PARKED_PATTERNS.search(text))


ORG_NUMBER_MENTION = re.compile(r"(?i)(?:org(?:anisasjons)?\.?\s*(?:nr|nummer|no|number)\.?|foretaksregisteret|\bNO)\s*[:.]?\s*(\d{3}[\s.]?\d{3}[\s.]?\d{3})(?!\d)")


def foreign_org_numbers(html: str, own: set[str]) -> set[str]:
    """Organisation numbers the page presents as its operator's that belong to no entity in `own`."""
    text = " ".join(re.sub(r"<[^>]+>", " ", html or "").split())
    found = {re.sub(r"\D", "", match.group(1)) for match in ORG_NUMBER_MENTION.finditer(text)}
    return {number for number in found if len(number) == 9} - own


def norway_tie(identifiers: Mapping[str, Any], html: str) -> tuple[str, str] | None:
    """(proof label, span) tying a non-.no site to Norway: the registered street and postcode, else a
    +47 phone, a .no email or a Norwegian page language. Used only with the unique-legal-name rule."""
    if span := address_span(identifiers, html):
        return "registry_address", span
    if span := norway_span(html):
        return "norway_contact", span
    return None


def without_domain_mentions(html: str, domain: str) -> str:
    """Page HTML with the site's own domain removed, so 'acme.no' printed on the page cannot count as
    the legal name 'ACME' appearing on it."""
    label = domain.split(".")[0]
    return re.sub(r"(?i)(https?://)?(www\.)?" + re.escape(label) + r"\.[a-z]{2,}(/\S*)?", " ", html or "")


def _fallback_kind(reason: str) -> str:
    """Classify an unreachable origin: 'timeout' (host effectively dead: try nothing else), 'tls'
    (certificate/TLS problem: plain HTTP may work), or 'other' (refused, reset: try the next host)."""
    lowered = reason.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "ssl" in lowered or "certificate" in lowered or "tls" in lowered:
        return "tls"
    return "other"


def contact_links(base_url: str, html: str, limit: int = PROOF_PAGE_LIMIT) -> list[str]:
    """Same-host proof-page links: contact first, then about, then legal (privacy/terms) pages."""
    base = urllib.parse.urlparse(base_url)
    terms = CONTACT_TERMS + LEGAL_TERMS
    ranked: dict[str, int] = {}
    for anchor in BeautifulSoup(html, "lxml").select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(terms) if term in haystack), None)
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if rank is None or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        ranked[clean] = min(rank, ranked.get(clean, rank))

    def category(rank: int) -> int:  # 0 contact, 1 about, 2 legal
        return 0 if rank < 2 else 1 if rank < len(CONTACT_TERMS) else 2

    ordered = sorted(ranked.items(), key=lambda item: (item[1], item[0]))
    picked: list[str] = []
    for wanted in (0, 1, 2):  # best link of each kind first, so legal pages are not crowded out
        best = next((url for url, rank in ordered if category(rank) == wanted and url not in picked), None)
        if best:
            picked.append(best)
    picked += [url for url, _ in ordered if url not in picked]
    return picked[:limit]


def _page_record(page: Page, spans: dict[str, str], name_span: str | None) -> dict[str, Any]:
    claim_spans = dict(spans)
    if name_span:
        claim_spans["legal_name_on_site"] = name_span
    return {
        "url": page.final_url,
        "requested_url": page.requested_url,
        "http_status": page.status,
        "retrieved_at": page.retrieved_at,
        "content_sha256": page.content_sha256,
        "proofs": sorted(claim_spans),
        "claim_spans": claim_spans,
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
    name_keys: Mapping[str, int] | None = None,
) -> tuple[dict[str, Any], Page | None]:
    """Find the entity's own website. Returns the evidence record and the verified homepage for reuse.

    States: available (official tie to this entity on the site), ambiguous (reachable but only an
    administrator/group site), not_available (no candidate proved), failed (budget exhausted).
    `name_keys` (registry namesake counts) enables the unique-full-legal-name rule; None disables it.
    """
    identifiers = registry_identifiers(row)
    legal_name = str(row.get("navn") or row.get("name") or "")
    full_labels = full_name_labels(legal_name)
    # Acronyms (USH, NGE) are too short to identify a company by name alone; they need a registry identifier.
    distinctive = name_key(legal_name)
    unique_name = (name_keys is not None and bool(distinctive) and name_keys.get(distinctive, 0) == 1
                   and len(distinctive.replace(" ", "")) >= MIN_UNIQUE_NAME_CHARS)
    attempts: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    fallback: dict[str, Any] | None = None
    hosts_fetched = 0
    try:
        for candidate in website_candidates(row, shared_domains):
            domain = candidate["domain"]
            if hosts_fetched >= max_hosts:
                break
            if domain in seen_domains:
                continue
            hosts = [name for name in (domain, "www." + domain) if resolver(name)]
            if not hosts:
                attempts.append({**candidate, "outcome": "no_dns"})
                continue
            # Small sites often serve only one of bare/www, or only plain HTTP (expired or missing TLS).
            # Fallbacks are tried only when the failure says they can help, so dead hosts stay cheap.
            reason_of = getattr(robots_allowed, "unreachable_reason", None)
            base_url, allowed, tls_failed = "", None, False
            for host in hosts:
                base_url = f"https://{host}/"
                allowed = robots_allowed(base_url)
                if allowed is not None:
                    break
                kind = _fallback_kind(reason_of(base_url)) if reason_of else "other"
                tls_failed = tls_failed or kind == "tls"
                if kind == "timeout":
                    break
            if allowed is None and (tls_failed or reason_of is None):
                for host in hosts:
                    base_url = f"http://{host}/"
                    allowed = robots_allowed(base_url)
                    if allowed is not None:
                        break
            if allowed is None:
                attempts.append({**candidate, "outcome": "unreachable", "url": base_url})
                continue
            if not allowed:
                attempts.append({**candidate, "outcome": "blocked_robots", "url": base_url})
                continue
            hosts_fetched += 1
            home = fetch(base_url)
            if home.error or not home.final_url:
                attempts.append({**candidate, "outcome": "fetch_failed", "url": base_url, "http_status": home.status, "error": home.error})
                continue
            final_domain = registered_domain(home.final_url)
            seen_domains.update({domain, final_domain})
            if is_parked(home.html):
                attempts.append({**candidate, "outcome": "parked", "url": home.final_url, "final_domain": final_domain})
                continue
            relation = candidate["relation"]
            if shared_domains.get(final_domain, 0) >= SHARED_DOMAIN_THRESHOLD:
                relation = "administrator_or_group"
            registry_declared = candidate["source"] == "registry_website"
            # Registry uniqueness says nothing about global names outside .no (Norid registers .no only to
            # Norwegian holders): elsewhere the name rule also needs the registered address on the site, and
            # the site's own domain printed on the page does not count as the name.
            is_no = final_domain.endswith(".no")
            label_match = final_domain.split(".")[0] in full_labels
            name_html = (lambda html: html) if is_no else (lambda html: without_domain_mentions(html, final_domain))
            address = norway_tie(identifiers, home.html)
            full_name_domain = label_match and (is_no or bool(address))
            name_suffices = registry_declared or (unique_name and full_name_domain and relation != "administrator_or_group")
            spans = page_proof_spans(identifiers, home.html, shared_phones=shared_phones)
            if address:
                spans[address[0]] = address[1]
            name_span = legal_name_span(legal_name, name_html(home.html))
            found = set(spans)
            proof_pages = [_page_record(home, spans, name_span)]
            own_numbers = {identifiers.get("organisation_number") or "", *(identifiers.get("subunit_numbers") or [])} - {""}
            foreign = foreign_org_numbers(home.html, own_numbers)
            if not found & STRONG_PROOFS and not (name_suffices and name_span and not foreign):
                for link in contact_links(home.final_url, home.html):
                    if not robots_allowed(link):
                        continue
                    page = fetch(link)
                    if page.error or registered_domain(page.final_url) != final_domain:
                        continue
                    page_spans = page_proof_spans(identifiers, page.html, shared_phones=shared_phones)
                    if not address and (page_address := norway_tie(identifiers, page.html)):
                        address = page_address
                        page_spans[page_address[0]] = page_address[1]
                        full_name_domain = label_match and (is_no or bool(address))
                        name_suffices = registry_declared or (unique_name and full_name_domain and relation != "administrator_or_group")
                    page_name = legal_name_span(legal_name, name_html(page.html))
                    foreign |= foreign_org_numbers(page.html, own_numbers)
                    proof_pages.append(_page_record(page, page_spans, page_name))
                    found |= set(page_spans)
                    name_span = name_span or page_name
                    if found & STRONG_PROOFS or (name_suffices and name_span and not foreign):
                        break
            assessment = assess_site_identity(
                found,
                relation,
                registry_declared=registry_declared,
                name_on_site=bool(name_span),
                full_name_domain=full_name_domain,
                unique_legal_name=unique_name,
            )
            if foreign and not found & STRONG_PROOFS:
                # The site names another entity as its operator and none of ours: never publish on name
                # or registry declaration alone (group, parent, previous owner or a namesake).
                assessment = {**assessment, "status": "related", "publishable": False,
                              "conflicting_organisation_numbers": sorted(foreign)}
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
    return _record("not_available", source_url=REGISTRY_SOURCE, retrieved_at=utc_now(), attempts=attempts, note="No candidate website carried an official tie to this entity"), None
