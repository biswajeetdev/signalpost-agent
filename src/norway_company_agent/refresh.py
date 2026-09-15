from __future__ import annotations

from typing import Any, Callable


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.website": ("website",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
}
# Social profiles are read from the verified site, so the website state decides whether both are comparable.
CONCLUSIVE_SITE_STATES = frozenset({"available", "not_available", "ambiguous"})
SITE_MODULES = ("website", "social_profiles")
SITE_FIELDS: dict[str, Callable[[dict[str, dict[str, Any]]], Any]] = {
    "website.official_url": lambda site: (site["website"].get("value") or {}).get("final_url") if site["website"].get("status") == "available" else None,
    "social_profiles.urls": lambda site: sorted(str(item.get("url")) for item in (site["social_profiles"].get("value") or {}).get("profiles") or []),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def _site_records(profile: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """This run's website and social records when the website check was conclusive, else the carried ones."""
    records = profile.get("evidence") or {}
    for prefix in ("", "carried_"):
        if (records.get(prefix + "website") or {}).get("status") in CONCLUSIVE_SITE_STATES:
            return {module: records.get(prefix + module) or {} for module in SITE_MODULES}
    return None


def carry_forward(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Keep the last conclusive site records in the profile snapshot when this run's website check was
    inconclusive (e.g. budget exhausted), so a failed run cannot hide a later real change. Envelopes never
    read the carried keys."""
    records = current.setdefault("evidence", {})
    if (records.get("website") or {}).get("status") in CONCLUSIVE_SITE_STATES:
        for module in SITE_MODULES:
            records.pop("carried_" + module, None)
        return current
    prior = _site_records(previous)
    if prior:
        for module in SITE_MODULES:
            records["carried_" + module] = prior[module]
    return current


def _change(org: str, field: str, old_value: Any, new_value: Any, record: dict[str, Any], previous_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "organisation_number": org,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "source_url": record.get("source_url"),
        "retrieved_at": record.get("retrieved_at"),
        "effective_at": record.get("effective_at") or record.get("as_of"),
        "source_class": record.get("source_class") or record.get("source_type"),
        "old_content_sha256": previous_record.get("content_sha256"),
        "new_content_sha256": record.get("content_sha256"),
        "status": record.get("status"),
    }


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if old_value != new_value:
            changes.append(_change(new_org, field, old_value, new_value, _evidence_for(current, field), _evidence_for(previous, field)))
    old_site, new_site = _site_records(previous), _site_records(current)
    if old_site and new_site:  # an inconclusive website check on either side is never a change
        for field, read in SITE_FIELDS.items():
            old_value, new_value = read(old_site), read(new_site)
            if old_value != new_value:
                module = field.split(".", 1)[0]
                changes.append(_change(new_org, field, old_value, new_value, new_site[module], old_site[module]))
    return changes


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]
