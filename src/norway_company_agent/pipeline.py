"""One company end to end: declared official cache, live annual accounts, website discovery,
company-linked profiles, and a terminal contract envelope for every input."""
from __future__ import annotations

import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import extruct
from bs4 import BeautifulSoup

from .budget import BudgetExhausted, RequestBudget, RobotsCache
from .cached_official import OfficialCache
from .contract import MODULES, build_envelope, validate_envelope
from .evidence import evidence, utc_now
from .http import FetchResult, fetch_json
from .jobs_nav import NavJobIndex, company_jobs, employer_homepages
from .arbeidsplassen import attach_board_ads
from .news_search import default_fetch as default_news_fetch, news_mentions
from .site_activity import site_activity
from .site_jobs import site_postings
from .search_candidates import search_domains, search_key
from .proof import STRONG_PROOFS
from .official import _reserve_history_slot, fetch_official_modules
from .refresh import carry_forward, diff_profile
from .site_discovery import Page, discover_website, make_site_fetchers
from .website import _social_links, structured_social_links

LIVE_OFFICIAL_MODULES = frozenset({"financials", "financial_history"})
HISTORY_PATH = "/aarsregnskap/kopi/"
HISTORY_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
HISTORY_SECONDS = 2.1  # matches official._reserve_history_slot
REGISTRY_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"


@dataclass(frozen=True)
class RunSettings:
    discovery_allowance: int = 14
    max_hosts: int = 4
    unique_name_rule: bool = True
    # Below this much wall clock, discovery is skipped so every company still gets an envelope.
    min_seconds_for_discovery: float = 120.0
    workers: int = 8
    # Watchdog limit for one chunk (None: only the run deadline applies), and the time kept free at the
    # end of the run to fill deferred records and write output.
    chunk_seconds: float | None = None
    output_margin_seconds: float = 90.0


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
    """The link exactly as the page writes it (the quoted URL containing the profile handle)."""
    handle = url.rstrip("/").rsplit("/", 1)[-1]
    if not handle:
        return url
    host = url.split("/")[2].removeprefix("www.").split(".")[0]
    for match in re.finditer(r"[\"']((?:https?:)?//[^\"'\s<>]*" + re.escape(handle) + r"[^\"'\s<>]*)[\"']", html, re.I):
        if host in match.group(1).lower():
            return match.group(0)[:240]
    return url


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


CONTACT_COLUMNS = (
    "epostadresse", "telefon", "mobil", "hjemmeside",
    "forretningsadresse.adresse", "forretningsadresse.postnummer", "forretningsadresse.poststed", "forretningsadresse.kommune",
)


TRANSIENT_OUTCOMES = frozenset({"unreachable", "fetch_failed"})


def carry_unreachable_site(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A site verified in the previous run that this run could not reach (network failure, not a failed
    identity check) keeps its last supported verification, with the failure exposed in the note; the
    playbook's 'failed refreshes keep the last known supported value'. Returns (website, social_profiles)."""
    prior = ((previous or {}).get("evidence") or {})
    prior_site = prior.get("website") or {}
    if current.get("status") == "available" or prior_site.get("status") != "available":
        return None
    prior_value = prior_site.get("value") or {}
    domain = str(prior_value.get("registered_domain") or "")
    attempts = [attempt for attempt in current.get("attempts") or [] if domain and domain in {attempt.get("domain"), attempt.get("final_domain")}]
    if not attempts or any(attempt.get("outcome") not in TRANSIENT_OUTCOMES for attempt in attempts):
        return None  # judged this run, or no longer a candidate under the current rules: the current verdict stands
    proofs = set(((prior_value.get("identity_assessment") or {}).get("proofs")) or [])
    still_valid = bool(proofs & STRONG_PROOFS) or "registry_declared_website" in proofs or (
        "unique_legal_name_domain" in proofs and domain.endswith(".no"))
    if not still_valid:
        return None  # the earlier proof would not pass today's identity rules
    outcome = attempts[-1].get("outcome")
    note = (f"Carried forward: verified {str(prior_site.get('retrieved_at'))[:10]}; this run could not reach "
            f"{domain} ({outcome}). Last supported value kept, failure exposed.")
    website = {**prior_site, "note": note, "carried_forward": True, "refresh_attempts": current.get("attempts") or []}
    social = {**(prior.get("social_profiles") or {}), "carried_forward": True}
    if social.get("status") is None:
        social = {"field": "social_profiles", "status": "not_available", "source_type": "company_owned_website", "source_class": "company_owned_website",
                  "source_url": prior_site.get("source_url"), "retrieved_at": prior_site.get("retrieved_at"), "note": "Carried forward with the website"}
    return website, social


def careers_record(home: Page | None, fetch: Any, robots_allowed: Any) -> dict[str, Any]:
    """Evidence record for postings listed on the verified company website (home + one careers page)."""
    if home is None:
        return evidence("site_jobs", "not_available", "company_owned_website", REGISTRY_SOURCE, note="No verified company website to read job postings from")
    postings, note, careers = site_postings(home, fetch, robots_allowed)
    if not postings and not careers:
        return evidence("site_jobs", "not_available", "company_owned_website", home.final_url, retrieved_at=home.retrieved_at,
                        content_sha256=home.content_sha256, note=note or "No job postings or careers page on the verified site")
    return evidence("site_jobs", "available", "company_owned_website", home.final_url, value={"ads": postings, "careers_page": careers},
                    retrieved_at=home.retrieved_at, content_sha256=home.content_sha256, note=note)


def with_subunits(row: Mapping[str, Any], locations: Mapping[str, Any] | None) -> dict[str, Any]:
    """Registry row plus the entity's registered subunits (same legal entity) for website discovery:
    their numbers are identity proofs on a site, their websites and names are candidates only."""
    items = ((locations or {}).get("value") or {}).get("locations") or [] if (locations or {}).get("status") == "available" else []
    if not isinstance(row, dict) or not items:
        return dict(row) if isinstance(row, dict) else {}
    return {
        **row,
        "_subunit_numbers": [str(item.get("organisation_number")) for item in items if item.get("organisation_number")],
        "_subunit_websites": [str(item.get("website")) for item in items if item.get("website")],
        "_subunit_names": [str(item.get("name")) for item in items if item.get("name") and str(item.get("name")).strip().upper() != str(row.get("navn") or "").strip().upper()],
    }


def fill_registry_contacts(records: dict[str, Any], org: str, fetch: Callable[[str], FetchResult]) -> None:
    """A bulk file without Brønnøysund contact columns (e.g. the flat company list) leaves the identity
    gate without the registry email/phone proofs; fill only the absent columns from the live entity."""
    registry = records.get("registry") or {}
    row = registry.get("value")
    if registry.get("status") != "available" or not isinstance(row, dict) or all(key in row for key in CONTACT_COLUMNS):
        return
    from .official import BRREG_ENTITY
    from .sampling import flatten_registry_object

    result = fetch(BRREG_ENTITY.format(org=org))
    if result.status != 200 or not isinstance(result.body, dict):
        return
    live = flatten_registry_object(result.body)
    filled = {key: live.get(key, "") for key in CONTACT_COLUMNS if key not in row}
    row.update(filled)
    registry["contact_fields_source"] = {"url": result.url, "retrieved_at": result.retrieved_at, "content_sha256": result.content_sha256, "fields": sorted(filled)}


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
    news_fetch: Callable[..., Any] = default_news_fetch,
    resolver: Callable[[str], bool] | None = None,
    jobs_index: NavJobIndex | None = None,
    previous_profile: Mapping[str, Any] | None = None,
    history_prefetch: Any = None,
    news_prefetch: Any = None,
    directory_prefetch: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    org = profile["organisation_number"]
    started = time.monotonic()
    records = profile.setdefault("evidence", {})
    fetcher = official_fetcher(budget, org)
    fill_registry_contacts(records, org, fetcher)
    records.update(cache.module_records(org, fetch=fetcher))
    official, _ = fetch_official_modules(org, {"financials"}, fetcher=fetcher)
    records.update(official)
    registry_row = with_subunits((records.get("registry") or {}).get("value") or {}, records.get("locations"))

    def add_jobs() -> None:
        records["jobs"] = company_jobs(
            org, str(registry_row.get("navn") or profile.get("name") or ""), records.get("locations"), jobs_index,
            spend=lambda: budget.spend(org, "jobs_detail"),
            wait_seconds=0.0,  # never block a worker on the feed; fill_deferred_jobs completes it after the batch
        )

    # With the feed index already read, jobs go first so NAV-listed employer homepages become website
    # candidates; while it is still being read in the background, website discovery does not wait for it.
    jobs_first = jobs_index is not None and jobs_index.ready.is_set()
    if jobs_first:
        add_jobs()
        homepages = employer_homepages(records["jobs"])
        if homepages and isinstance(registry_row, dict):
            registry_row = {**registry_row, "_nav_employer_homepages": homepages}
    api_key = search_key()
    if api_key and isinstance(registry_row, dict) and not registry_row.get("hjemmeside") and budget.seconds_left() > settings.min_seconds_for_discovery:
        try:
            found = search_domains(registry_row, api_key, spend=lambda: budget.spend(org, "search_api"))
        except BudgetExhausted:
            found = []
        if found:
            registry_row = {**registry_row, "_search_domains": found}
    if directory_prefetch is not None and isinstance(registry_row, dict) and not registry_row.get("hjemmeside"):
        listed = directory_prefetch.candidates(org)  # never waits: used only if the lookup already finished
        if listed:
            registry_row = {**registry_row, "_directory_domains": listed}
    home: Page | None = None
    if budget.seconds_left() < settings.min_seconds_for_discovery:
        records["website"] = evidence("website", "failed", "website_candidate_search", REGISTRY_SOURCE, note="Skipped: run wall-clock budget nearly exhausted")
    else:
        fetch, robots_allowed = site_fetchers(budget, org, allowance=settings.discovery_allowance, robots=robots)
        records["website"], home = discover_website(
            registry_row,
            shared_domains=shared["domains"],
            shared_phones=shared["phones"],
            fetch=fetch,
            robots_allowed=robots_allowed,
            max_hosts=settings.max_hosts,
            name_keys=shared["names"] if settings.unique_name_rule else None,
            **({"resolver": resolver} if resolver else {}),
        )
    carried = carry_unreachable_site(previous_profile, records["website"])
    if carried:
        records["website"], records["social_profiles"] = carried
    else:
        records["social_profiles"] = social_profiles(records["website"], home)
    if records["website"].get("status") == "available" and isinstance(records["website"].get("value"), dict):
        # The starter kit's profile layout keeps company-linked profiles on the website record as well.
        records["website"]["value"]["social_links"] = [
            {"platform": item.get("platform"), "url": item.get("url")}
            for item in ((records["social_profiles"].get("value") or {}).get("profiles") or [])
        ] if records["social_profiles"].get("status") == "available" else []
    if records["website"].get("status") == "available" and home is not None and budget.seconds_left() > settings.min_seconds_for_discovery:
        # A few requests beyond discovery: one news/press index on the verified site (plus its robots.txt),
        # then, only if it shows no dates, the site's feed or sitemap and up to five article pages.
        activity_fetch, activity_robots = site_fetchers(budget, org, allowance=budget.by_company[org] + 10, robots=robots)
        records["public_activity"] = site_activity(records["website"], home, activity_fetch, activity_robots)
        # And the careers page on the verified site (one level deeper if it lists no ads), plus the recruitment-host
        # page the site links to: postings the company itself lists.
        jobs_fetch, jobs_robots = site_fetchers(budget, org, allowance=budget.by_company[org] + 7, robots=robots)
        records["site_jobs"] = careers_record(home, jobs_fetch, jobs_robots)
    else:
        records["public_activity"] = site_activity(records["website"], None, None, None)
        records["site_jobs"] = careers_record(None, None, None)
    if news_prefetch is not None:
        # Searched on the prefetch threads; never wait for it here (see finalize_deferred).
        records["news_mentions"] = news_prefetch.record(org) or {
            **evidence("news_mentions", "failed", "independent_news_discovery", "https://www.bing.com/news/search",
                       note="Deferred: filled from the paced news search after the batch"), "deferred": True}
    elif budget.seconds_left() > settings.min_seconds_for_discovery:
        records["news_mentions"] = news_mentions(
            org, str(registry_row.get("navn") or profile.get("name") or ""), shared["names"],
            spend=lambda: budget.spend(org, "news_search"), fetch=news_fetch,
        )
    if jobs_index is not None and not jobs_first:
        add_jobs()
    # The annual-account copy endpoint is paced run-wide (one start per HISTORY_SECONDS); fetch it last,
    # and only while the remaining paced backlog fits the time budget.
    if history_prefetch is not None:
        # Fetched on the prefetch thread; never wait for it here (see finalize_deferred).
        records["financial_history"] = history_prefetch.record(org) or {
            **evidence("financial_history", "failed", "official_annual_account_copies", HISTORY_URL.format(org=org),
                       note="Deferred: filled from the paced history stream after the batch"), "deferred": True}
    elif budget.take_paced_slot(HISTORY_SECONDS):
        history, _ = fetch_official_modules(org, {"financial_history"}, fetcher=fetcher)
        records.update(history)
    else:
        records["financial_history"] = evidence(
            "financial_history", "failed", "official_annual_account_copies", HISTORY_URL.format(org=org),
            note="Deferred: the rate-limited annual-account copy endpoint would not fit this run's time budget",
        )
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
            prior = (previous or {}).get(profile["organisation_number"])
            return enrich_company(profile, cache=cache, budget=budget, robots=robots, settings=settings, shared=shared,
                                  previous_profile=prior, **enrich_overrides)
        except Exception as exc:  # every input still ends in a terminal envelope
            return _mark_failed(profile, exc), {"requests": budget.by_company[profile["organisation_number"]], "runtime_ms": 0, "third_party_cost_usd": 0}

    # Watchdog: a chunk never runs past its own limit or the run deadline (minus a margin to write output).
    # Companies still unfinished then get an honest failed envelope from a copy of their input, and the
    # pool is released without waiting; stuck threads cannot hold the run (the runner exits hard).
    limit = budget.seconds_left() - settings.output_margin_seconds
    if settings.chunk_seconds:
        limit = min(limit, settings.chunk_seconds)
    pool = ThreadPoolExecutor(max_workers=settings.workers)
    futures = {pool.submit(work, profile): profile for profile in profiles}
    done, pending = wait(futures, timeout=max(limit, 1.0))
    for future in done:
        profile, operations = future.result()
        results[profile["organisation_number"]] = (profile, operations)
    for future in pending:
        original = futures[future]
        org = original["organisation_number"]
        future.cancel()
        stalled = _mark_failed(deepcopy(original), TimeoutError(f"chunk watchdog: unfinished after {max(limit, 1.0):.0f}s"))
        results[org] = (stalled, {"requests": budget.by_company[org], "runtime_ms": int(max(limit, 1.0) * 1000), "third_party_cost_usd": 0, "watchdog": True})
    pool.shutdown(wait=not pending, cancel_futures=True)
    completed_at = utc_now()
    envelopes = []
    for profile in profiles:
        org = profile["organisation_number"]
        enriched, operations = results[org]
        changes = diff_profile(dict(previous[org]), carry_forward(previous[org], enriched)) if previous and org in previous else []
        envelopes.append(build_envelope(enriched, run_id=run_id, started_at=started_at, completed_at=completed_at, operations=operations, changes=changes))
    return envelopes, [results[profile["organisation_number"]][0] for profile in profiles], batch_report(envelopes, budget, started_at, completed_at)


def fill_deferred_jobs(
    envelopes: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    *,
    jobs_index: NavJobIndex,
    budget: RequestBudget,
    margin_seconds: float = 30.0,
    previous: Mapping[str, dict[str, Any]] | None = None,
) -> int:
    """Companies processed before the NAV feed index was ready carry a deferred jobs record. Wait for
    the index only as long as the time budget allows, then fill those records and rebuild just their
    envelopes (same run metadata, operations and changes). Returns the number of envelopes rebuilt."""
    deferred = [index for index, profile in enumerate(profiles) if ((profile.get("evidence") or {}).get("jobs") or {}).get("deferred")]
    if not deferred:
        return 0
    jobs_index.ready.wait(max(budget.seconds_left() - margin_seconds, 0.0))
    for position in deferred:
        profile, envelope = profiles[position], envelopes[position]
        org = profile["organisation_number"]
        records = profile["evidence"]
        registry_row = (records.get("registry") or {}).get("value") or {}
        if jobs_index.ready.is_set():
            try:
                records["jobs"] = company_jobs(org, str(registry_row.get("navn") or profile.get("name") or ""), records.get("locations"), jobs_index,
                                               spend=lambda org=org: budget.spend(org, "jobs_detail"))
            except BudgetExhausted as exc:
                records["jobs"] = evidence("jobs", "failed", "nav_public_job_feed", "https://pam-stilling-feed.nav.no/api/v1/feed", note=f"Run budget exhausted before job ads were checked: {exc}")
        else:
            records["jobs"] = evidence("jobs", "failed", "nav_public_job_feed", "https://pam-stilling-feed.nav.no/api/v1/feed", note="NAV job feed read did not finish within the run time budget")
        _rebuild(envelopes, profiles, position, budget, previous)
    return len(deferred)


def finalize_deferred(
    envelopes: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    *,
    budget: RequestBudget,
    history_prefetch: Any = None,
    jobs_index: NavJobIndex | None = None,
    margin_seconds: float = 30.0,
    news_prefetch: Any = None,
    board_prefetch: Any = None,
    previous: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Fill records deferred during the batch (paced filing history, news search, NAV jobs, job-board ads) within the time left, then
    rebuild only the affected envelopes. Returns counts of rebuilt envelopes per source."""
    counts = {"history": 0, "jobs": 0, "news": 0, "board": 0}
    if history_prefetch is not None:
        history_prefetch.done.wait(max(budget.seconds_left() - margin_seconds, 0.0))
        history_prefetch.stop()
        for position, profile in enumerate(profiles):
            records = profile.get("evidence") or {}
            if (records.get("financial_history") or {}).get("deferred"):
                records["financial_history"] = history_prefetch.final_record(profile["organisation_number"])
                counts["history"] += 1
                _rebuild(envelopes, profiles, position, budget, previous)
    if news_prefetch is not None:
        news_prefetch.done.wait(max(budget.seconds_left() - margin_seconds, 0.0))
        news_prefetch.stop()
        for position, profile in enumerate(profiles):
            records = profile.get("evidence") or {}
            if (records.get("news_mentions") or {}).get("deferred"):
                records["news_mentions"] = news_prefetch.final_record(profile["organisation_number"])
                counts["news"] += 1
                _rebuild(envelopes, profiles, position, budget, previous)
    if jobs_index is not None:
        counts["jobs"] = fill_deferred_jobs(envelopes, profiles, jobs_index=jobs_index, budget=budget, previous=previous)
    if board_prefetch is not None:
        # Job-board ads are matched here, after the feed's own fill, against the entity and its subunits.
        board_prefetch.done.wait(max(budget.seconds_left() - margin_seconds, 0.0))
        board_prefetch.stop()
        for position, profile in enumerate(profiles):
            records = profile.get("evidence") or {}
            updated = attach_board_ads(records.get("jobs"), board_prefetch.record(profile["organisation_number"]),
                                       profile["organisation_number"], records.get("locations"))
            if updated is not None:
                records["jobs"] = updated
                counts["board"] += 1
                _rebuild(envelopes, profiles, position, budget, previous)
    return counts


def _rebuild(envelopes: list[dict[str, Any]], profiles: list[dict[str, Any]], position: int, budget: RequestBudget,
             previous: Mapping[str, dict[str, Any]] | None = None) -> None:
    """Rebuild one envelope after a deferred record was filled. Changes are diffed again against the
    previous profile: the batch-time diff saw the deferred placeholder, not the filled record."""
    envelope, profile = envelopes[position], profiles[position]
    org = profile["organisation_number"]
    changes = diff_profile(dict(previous[org]), carry_forward(previous[org], profile)) if previous and org in previous else envelope.get("changes") or []
    run = envelope.get("run") or {}
    envelopes[position] = build_envelope(profile, run_id=run.get("run_id"), started_at=run.get("started_at"), completed_at=run.get("completed_at"),
                                         operations={**(envelope.get("operations") or {}), "requests": budget.by_company[org]},
                                         changes=changes)


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
