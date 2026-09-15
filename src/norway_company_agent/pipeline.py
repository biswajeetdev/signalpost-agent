"""One company end to end: declared official cache, live annual accounts, website discovery,
company-linked profiles, and a terminal contract envelope for every input."""
from __future__ import annotations

import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import extruct
from bs4 import BeautifulSoup

from .budget import BudgetExhausted, RequestBudget, RobotsCache
from .cached_official import OfficialCache
from .contract import MODULES, build_envelope, validate_envelope
from .evidence import evidence, utc_now
from .http import FetchResult, fetch_json
from .official import _reserve_history_slot, fetch_official_modules
from .refresh import carry_forward, diff_profile
from .site_discovery import Page, discover_website, make_site_fetchers
from .website import _social_links, structured_social_links

LIVE_OFFICIAL_MODULES = frozenset({"financials", "financial_history"})
HISTORY_PATH = "/aarsregnskap/kopi/"
REGISTRY_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"


@dataclass(frozen=True)
class RunSettings:
    discovery_allowance: int = 14
    max_hosts: int = 4
    unique_name_rule: bool = True
    # Below this much wall clock, discovery is skipped so every company still gets an envelope.
    min_seconds_for_discovery: float = 120.0
    workers: int = 8


def budgeted_official_fetcher(budget: RequestBudget, company: str) -> Callable[[str], FetchResult]:
    def fetch(url: str) -> FetchResult:
        if HISTORY_PATH in url:
            _reserve_history_slot()  # keep the annual-account copy endpoint under its rate allowance
        try:
            return fetch_json(url, attempts=2, on_attempt=lambda: budget.spend(company, "official", essential=True))
        except BudgetExhausted as exc:
            return FetchResult(url, 0, 0, 0, error=str(exc), retrieved_at=utc_now())

    return fetch


def _link_span(html: str, url: str) -> str:
    handle = url.rstrip("/").rsplit("/", 1)[-1]
    position = html.lower().find(handle.lower()) if handle else -1
    if position < 0:
        return url
    window = html[max(0, position - 120): position + len(handle) + 40]
    return " ".join(re.sub(r"<[^>]*>?", " ", window).split())[:240] or url


def social_profiles(website: Mapping[str, Any], home: Page | None) -> dict[str, Any]:
    """Profiles linked from the verified company website only; name-similar handles are never used."""
    if website.get("status") != "available" or home is None or not home.final_url:
        return evidence("social_profiles", "not_available", "company_owned_website", str(website.get("source_url") or REGISTRY_SOURCE), note="No verified company website to take company-linked profiles from")
    links = _social_links(home.final_url, BeautifulSoup(home.html, "lxml"))
    try:
        links += structured_social_links(extruct.extract(home.html, base_url=home.final_url, syntaxes=["json-ld"]).get("json-ld"))
    except Exception:
        pass
    unique = {item["url"]: item for item in links}
    profiles = [{**item, "claim_span": _link_span(home.html, item["url"])} for _, item in sorted(unique.items())]
    return evidence(
        "social_profiles", "available" if profiles else "not_available", "company_owned_website", home.final_url,
        value={"profiles": profiles}, content_sha256=home.content_sha256, retrieved_at=home.retrieved_at,
        note=None if profiles else "Verified company website links no social profiles",
    )


def enrich_company(
    profile: dict[str, Any],
    *,
    cache: OfficialCache,
    budget: RequestBudget,
    robots: RobotsCache,
    settings: RunSettings,
    shared: Mapping[str, Mapping[str, int]],
    official_fetcher: Callable[[RequestBudget, str], Callable[[str], FetchResult]] = budgeted_official_fetcher,
    site_fetchers: Callable[..., Any] = make_site_fetchers,
    resolver: Callable[[str], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    org = profile["organisation_number"]
    started = time.monotonic()
    records = profile.setdefault("evidence", {})
    records.update(cache.module_records(org))
    official, _ = fetch_official_modules(org, set(LIVE_OFFICIAL_MODULES), fetcher=official_fetcher(budget, org))
    records.update(official)
    home: Page | None = None
    if budget.seconds_left() < settings.min_seconds_for_discovery:
        records["website"] = evidence("website", "failed", "website_candidate_search", REGISTRY_SOURCE, note="Skipped: run wall-clock budget nearly exhausted")
    else:
        fetch, robots_allowed = site_fetchers(budget, org, allowance=settings.discovery_allowance, robots=robots)
        records["website"], home = discover_website(
            (records.get("registry") or {}).get("value") or {},
            shared_domains=shared["domains"],
            shared_phones=shared["phones"],
            fetch=fetch,
            robots_allowed=robots_allowed,
            max_hosts=settings.max_hosts,
            name_keys=shared["names"] if settings.unique_name_rule else None,
            **({"resolver": resolver} if resolver else {}),
        )
    records["social_profiles"] = social_profiles(records["website"], home)
    return profile, {"requests": budget.by_company[org], "runtime_ms": int((time.monotonic() - started) * 1000), "third_party_cost_usd": 0}


def _mark_failed(profile: dict[str, Any], error: Exception) -> dict[str, Any]:
    records = profile.setdefault("evidence", {})
    for module in MODULES:
        if module not in records:
            records[module] = evidence(module, "failed", "pipeline", REGISTRY_SOURCE, note=f"{type(error).__name__}: {str(error)[:200]}")
    return profile


def run_batch(
    profiles: list[dict[str, Any]],
    *,
    cache: OfficialCache,
    budget: RequestBudget,
    run_id: str,
    settings: RunSettings = RunSettings(),
    previous: Mapping[str, Mapping[str, Any]] | None = None,
    robots: RobotsCache | None = None,
    **enrich_overrides: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Enrich every profile and return (envelopes, profiles, report); one envelope per input, in input order."""
    started_at = utc_now()
    robots = RobotsCache() if robots is None else robots
    shared = {"domains": cache.shared_domains(), "phones": cache.shared_phones(), "names": cache.name_keys()}
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def work(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return enrich_company(profile, cache=cache, budget=budget, robots=robots, settings=settings, shared=shared, **enrich_overrides)
        except Exception as exc:  # every input still ends in a terminal envelope
            return _mark_failed(profile, exc), {"requests": budget.by_company[profile["organisation_number"]], "runtime_ms": 0, "third_party_cost_usd": 0}

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        for profile, operations in pool.map(work, profiles):
            results[profile["organisation_number"]] = (profile, operations)
    completed_at = utc_now()
    envelopes = []
    for profile in profiles:
        org = profile["organisation_number"]
        enriched, operations = results[org]
        changes = diff_profile(dict(previous[org]), carry_forward(previous[org], enriched)) if previous and org in previous else []
        envelopes.append(build_envelope(enriched, run_id=run_id, started_at=started_at, completed_at=completed_at, operations=operations, changes=changes))
    return envelopes, [results[profile["organisation_number"]][0] for profile in profiles], batch_report(envelopes, budget, started_at, completed_at)


def batch_report(envelopes: Iterable[Mapping[str, Any]], budget: RequestBudget, started_at: str, completed_at: str) -> dict[str, Any]:
    envelopes = list(envelopes)
    problems = {item["organisation_number"]: found for item in envelopes if (found := validate_envelope(item))}
    runtimes = sorted(item["operations"]["runtime_ms"] for item in envelopes)
    module_states: dict[str, Counter[str]] = {}
    for item in envelopes:
        for module, state in item["modules"].items():
            module_states.setdefault(module, Counter())[state] += 1
    organisations = [item["organisation_number"] for item in envelopes]
    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "envelopes": len(envelopes),
        "unique_organisations": len(set(organisations)) == len(organisations),
        "module_states": {module: dict(counts) for module, counts in module_states.items()},
        "available_claims": dict(Counter(claim["field"] for item in envelopes for claim in item["claims"] if claim["availability"] == "available")),
        "operations": {
            **budget.report(),
            "runtime_ms_p50": runtimes[len(runtimes) // 2] if runtimes else None,
            "runtime_ms_p95": runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None,
            "third_party_cost_usd": 0,
        },
        "validation": {"passed": not problems, "problems": dict(list(problems.items())[:20]), "envelopes_with_problems": len(problems)},
    }
