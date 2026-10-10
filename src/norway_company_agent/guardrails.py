"""Post-run guard rails, from Builderr's official-run checks (evaluation contract) plus silent-failure
signals. Contract checks are hard: they mirror what makes a run unscorable or penalised. Silent-failure
checks are warnings: the run is valid, but coverage was lost to time, budget or upstream failures."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from .contract import CONTRACT_STATES, validate_envelope

STRONG_OR_ALLOWED = {"organisation_number", "subunit_organisation_number", "registry_email", "registry_phone", "registry_declared_website"}
OFFICIAL_MODULES = ("registry", "financials", "roles", "locations")
# Warning thresholds: share of companies with this module failed.
FAIL_RATE_WARN = {"registry": 0.01, "financials": 0.02, "roles": 0.02, "locations": 0.02, "financial_history": 0.05, "website": 0.05, "jobs": 0.10,
                  "news_mentions": 0.05}


def _site_basis_ok(website: Mapping[str, Any]) -> bool:
    value = website.get("value") or {}
    proofs = set((value.get("identity_assessment") or {}).get("proofs") or [])
    domain = str(value.get("registered_domain") or "")
    if proofs & STRONG_OR_ALLOWED:
        return True
    if "unique_legal_name_domain" in proofs:
        return domain.endswith(".no") or bool(proofs & {"registry_address", "norway_contact"})
    return False


def enforce_site_basis(envelopes: list[dict[str, Any]], profiles: list[dict[str, Any]], budget: Any) -> int:
    """Downgrade any published website lacking an allowed identity basis to ambiguous (and its company-linked
    profiles to not_available), rebuilding only those envelopes. Returns the number downgraded."""
    from .contract import build_envelope

    demoted = 0
    for position, profile in enumerate(profiles):
        records = profile.get("evidence") or {}
        website = records.get("website") or {}
        if website.get("status") != "available" or _site_basis_ok(website):
            continue
        records["website"] = {**website, "status": "ambiguous", "note": "Guard rail: published site lacked an allowed identity basis; withheld"}
        for module in ("social_profiles", "public_activity", "site_jobs"):
            if (records.get(module) or {}).get("status") == "available":
                records[module] = {**records[module], "status": "not_available", "value": None, "note": "Withheld with the unverified website"}
        envelope = envelopes[position]
        run = envelope.get("run") or {}
        envelopes[position] = build_envelope(profile, run_id=run.get("run_id"), started_at=run.get("started_at"), completed_at=run.get("completed_at"),
                                             operations=envelope.get("operations") or {}, changes=envelope.get("changes") or [])
        demoted += 1
    return demoted


def check_run(envelopes: list[Mapping[str, Any]], profiles: list[Mapping[str, Any]], organisations: list[str]) -> dict[str, Any]:
    hard: list[str] = []
    warnings: list[str] = []

    # 1. Exactly one terminal envelope per input, in input order, unique.
    orgs = [item.get("organisation_number") for item in envelopes]
    if len(envelopes) != len(organisations):
        hard.append(f"envelope count {len(envelopes)} != inputs {len(organisations)}")
    if orgs != organisations:
        hard.append("envelopes are not one-per-input in input order")
    if len(set(orgs)) != len(orgs):
        hard.append("duplicate organisation numbers among envelopes")

    # 2. Valid states, evidence on every published claim, reporting periods, no values on unpublished claims.
    envelope_problems = Counter()
    bad_states = Counter()
    duplicate_claims = 0
    for item in envelopes:
        for problem in validate_envelope(item):
            envelope_problems[problem.split(":", 1)[-1].strip()[:60]] += 1
        for module, state in (item.get("modules") or {}).items():
            if state not in CONTRACT_STATES:
                bad_states[module] += 1
        seen = set()
        for claim in item.get("claims") or []:
            key = (claim.get("field"), repr(claim.get("value")), claim.get("availability"))
            if claim.get("availability") == "available" and key in seen:
                duplicate_claims += 1
            seen.add(key)
    if envelope_problems:
        hard.append(f"envelope validation problems: {dict(envelope_problems.most_common(5))}")
    if bad_states:
        hard.append(f"invalid module states: {dict(bad_states)}")
    if duplicate_claims:
        warnings.append(f"{duplicate_claims} duplicate available claims")

    # 3. Exact identity: every published website rests on an allowed proof basis.
    weak_sites = [p["organisation_number"] for p in profiles
                  if ((p.get("evidence") or {}).get("website") or {}).get("status") == "available"
                  and not _site_basis_ok(p["evidence"]["website"])]
    if weak_sites:
        hard.append(f"{len(weak_sites)} published websites without an allowed identity basis (e.g. {weak_sites[:3]})")

    # 4. Silent failures: coverage lost to time, budget or upstream errors.
    total = max(len(profiles), 1)
    failed: dict[str, Counter] = {}
    for profile in profiles:
        for module, record in (profile.get("evidence") or {}).items():
            if isinstance(record, dict) and record.get("status") in {"failed", "source_error"}:
                failed.setdefault(module, Counter())[str(record.get("note") or record.get("status"))[:70]] += 1
    rates = {module: round(sum(reasons.values()) / total, 4) for module, reasons in failed.items()}
    for module, limit in FAIL_RATE_WARN.items():
        if rates.get(module, 0) > limit:
            top = failed[module].most_common(1)[0][0]
            warnings.append(f"{module}: {rates[module]:.1%} failed (top reason: {top})")
    deferred_left = sum(1 for p in profiles for m in ("financial_history", "jobs", "news_mentions")
                        if ((p.get("evidence") or {}).get(m) or {}).get("deferred"))
    if deferred_left:
        warnings.append(f"{deferred_left} deferred records were never filled")
    watchdog = sum(1 for item in envelopes if (item.get("operations") or {}).get("watchdog"))
    if watchdog:
        warnings.append(f"{watchdog} companies stopped by the chunk watchdog")
    budget_refusals = sum(reasons[r] for m in OFFICIAL_MODULES for reasons in [failed.get(m, Counter())] for r in reasons if "budget" in r.lower())
    if budget_refusals:
        warnings.append(f"{budget_refusals} official lookups refused because the run budget was exhausted")
    blocked_news = sum(1 for p in profiles if ((p.get("evidence") or {}).get("news_mentions") or {}).get("status") == "blocked")
    if blocked_news:
        warnings.append(f"news search blocked for {blocked_news} companies (robots.txt or unreachable)")
    skipped_sites = sum(n for r, n in failed.get("website", Counter()).items() if "Skipped" in r or "time" in r.lower())
    if skipped_sites:
        warnings.append(f"{skipped_sites} website checks skipped or cut for time")

    return {
        "contract_passed": not hard,
        "contract_failures": hard,
        "warnings": warnings,
        "failure_rates": rates,
        "failure_reasons": {module: dict(reasons.most_common(3)) for module, reasons in failed.items()},
        "watchdog_stopped": watchdog,
        "deferred_unfilled": deferred_left,
        "published_websites": sum(1 for p in profiles if ((p.get("evidence") or {}).get("website") or {}).get("status") == "available"),
    }


def stream_warnings(report: Mapping[str, Any]) -> list[str]:
    """Run-level sources that degrade without failing any single record: say so in the report."""
    warnings: list[str] = []
    for name in ("news_stream", "job_board_stream"):
        stream = report.get(name) or {}
        if stream and stream.get("searched", 0) < stream.get("of", 0):
            warnings.append(f"{name}: searched {stream.get('searched')} of {stream.get('of')} companies" + (f" ({stream['note']})" if stream.get("note") else ""))
    directory = report.get("directory_stream") or {}
    if directory and (directory.get("note") or directory.get("failed") or directory.get("looked_up", 0) < directory.get("of", 0)):
        warnings.append(f"directory_stream: looked up {directory.get('looked_up')} of {directory.get('of')}, {directory.get('failed')} failed"
                        + (f" ({directory['note']})" if directory.get("note") else ""))
    feed = report.get("jobs_feed") or {}
    if feed and feed.get("state") != "available":
        warnings.append(f"NAV job feed {feed.get('state')}: {feed.get('note')}")
    elif feed.get("note"):
        warnings.append(f"NAV job feed incomplete: {feed.get('note')}")
    history = report.get("history_stream") or {}
    if history and history.get("fetched", 0) < history.get("of", 0):
        warnings.append(f"filing history fetched for {history.get('fetched')} of {history.get('of')} companies")
    if history.get("errors"):
        warnings.append(f"filing history stream: {history['errors']} companies failed with an error")
    return warnings
