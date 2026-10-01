"""Optional search-API website candidates (dormant unless BRAVE_SEARCH_API_KEY is set, e.g. a key
Builderr supplies for runs). Search results only nominate domains; every domain still goes through the
same site-identity check as registry and name-guess candidates. Rank is never evidence."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from .candidates import registered_domain
from .http import read_bounded

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
MAX_SEARCH_DOMAINS = 3
# Directories, registries, maps, social networks and marketplaces: never the company's own site.
NOT_OWN_SITE = {
    "proff.no", "purehelp.no", "gulesider.no", "1881.no", "brreg.no", "regnskapstall.no", "forvalt.no", "bedriftsdatabasen.no",
    "allabolag.se", "kompass.com", "dnb.com", "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "finn.no", "google.com", "wikipedia.org", "yelp.com", "tripadvisor.com", "tripadvisor.no",
    "booking.com", "nav.no", "arbeidsplassen.no", "jobbnorge.no", "indeed.com", "glassdoor.com", "bing.com", "kart.gulesider.no",
    "virk.no", "firmainfo.no", "infobel.com", "opencorporates.com", "northdata.com", "eniro.no", "mittanbud.no", "anbud365.no",
}


def search_key() -> str | None:
    return os.environ.get(API_KEY_ENV, "").strip() or None


def build_query(row: Mapping[str, Any]) -> str:
    name = str(row.get("navn") or row.get("name") or "").strip()
    place = str(row.get("forretningsadresse.poststed") or row.get("forretningsadresse.kommune") or row.get("municipality") or "").strip()
    return f'"{name}" {place}'.strip()


def search_domains(row: Mapping[str, Any], api_key: str, *, spend: Callable[[], None] | None = None, timeout: float = 10.0) -> list[str]:
    """Up to MAX_SEARCH_DOMAINS candidate registered domains from one web search; [] on any failure."""
    if spend:
        spend()
    url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode({"q": build_query(row), "count": 10, "country": "no", "search_lang": "nb",
                                                           "safesearch": "moderate", "spellcheck": "0"})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
                                                   "X-Subscription-Token": api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(read_bounded(response, 2_000_000, 20.0))
    except Exception:
        return []
    domains: list[str] = []
    for result in ((payload.get("web") or {}).get("results") or []):
        domain = registered_domain(str(result.get("url") or ""))
        if domain and domain not in NOT_OWN_SITE and not any(domain.endswith("." + blocked) for blocked in NOT_OWN_SITE) and domain not in domains:
            domains.append(domain)
        if len(domains) >= MAX_SEARCH_DOMAINS:
            break
    return domains
