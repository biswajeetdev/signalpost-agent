from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


ORG_KEYS = (
    "organisation_number", "organization_number", "organisasjonsnummer", "orgnr", "org_nr",
    "org_number", "orgNumber", "organisationNumber", "organizationNumber", "org", "id",
)
LIST_KEYS = ("organisation_numbers", "organization_numbers", "organisations", "organizations", "companies", "orgs", "items", "batch", "data")


def _org_from(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ORG_KEYS:
            if value.get(key) not in (None, ""):
                return value[key]
        return None
    return value


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    """The evaluator-supplied batch: JSON (list or wrapping object), JSONL, CSV with a header, or plain
    text with one number per line; gzip or not. Order is preserved; a header line is skipped."""
    import csv
    import io

    from .sampling import open_text

    with open_text(path) as handle:
        text = handle.read()
    stripped = text.lstrip()
    values: list[Any]
    if stripped.startswith("[") or (stripped.startswith("{") and not _looks_jsonl(stripped)):
        body = json.loads(stripped)
        if isinstance(body, dict):
            values = next((body[key] for key in LIST_KEYS if isinstance(body.get(key), list)), [body] if _org_from(body) else [])
        else:
            values = body
    elif stripped.startswith("{"):
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        lines = [line for line in text.splitlines() if line.strip()]
        first = lines[0] if lines else ""
        if any(separator in first for separator in (",", ";", "\t")) or not any(ch.isdigit() for ch in first):
            dialect = csv.Sniffer().sniff(first, delimiters=",;\t") if any(s in first for s in ",;\t") else csv.excel
            reader = list(csv.reader(io.StringIO("\n".join(lines)), dialect=dialect))
            header = [cell.strip() for cell in reader[0]] if reader else []
            column = next((header.index(key) for key in ORG_KEYS if key in header), None)
            if column is not None:
                values = [row[column] for row in reader[1:] if len(row) > column]
            else:
                # No recognised header: take the first cell of each row, skipping a non-numeric header.
                values = [row[0] for row in reader if row and sum(ch.isdigit() for ch in row[0]) >= 9]
        else:
            values = [line.strip() for line in lines]
    records = []
    for value in values:
        org = _org_from(value)
        org = "".join(character for character in str(org or "") if character.isdigit())
        if len(org) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {value!r}")
        record = {"organisation_number": org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    orgs = [record["organisation_number"] for record in records]
    if len(orgs) != len(set(orgs)):
        raise ValueError("Organisation-number input contains duplicates")
    return records


def _looks_jsonl(text: str) -> bool:
    first_line = text.split("\n", 1)[0]
    try:
        json.loads(first_line)
    except json.JSONDecodeError:
        return False
    return "\n" in text.strip()


def exact_name(name: str) -> str:
    return " ".join(name.casefold().split())


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str], name_counts: "Counter[str] | None" = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Requested registry rows from the bulk snapshot. With `name_counts`, every row is read and the
    count of each exact legal name (casefolded, spacing normalised) is added to it."""
    requested = list(organisation_numbers)
    wanted = set(requested)
    snapshot_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if name_counts is not None:
            name_counts[exact_name(str(profile.get("name") or ""))] += 1
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        misaligned = profile.pop("csv_row_misaligned", False)
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "source_error" if misaligned else "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=None if misaligned else raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
                note="Bulk CSV row has shifted columns; registry fields withheld" if misaligned else None,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted) and name_counts is None:
            break
    missing = [org for org in requested if org not in found]
    for org in missing:
        # Deleted or re-registered after the universe froze: still one terminal envelope, never a crash.
        found[org] = {
            "organisation_number": org,
            "evidence": {
                "registry": evidence(
                    "registry",
                    "not_found",
                    "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    retrieved_at=retrieved_at,
                    content_sha256=snapshot_sha256,
                    source_row_key=org,
                    note=f"Absent from registry bulk snapshot {snapshot_sha256[:12]} (deleted or not yet registered)",
                ),
            },
        }
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(requested) - len(missing),
        "absent_from_snapshot": missing,
    }


def profiles_from_live_registry(organisation_numbers: Iterable[str], workers: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Registry rows from the live per-entity endpoint, for runs where no bulk snapshot is supplied."""
    from concurrent.futures import ThreadPoolExecutor

    from .http import fetch_json
    from .official import BRREG_ENTITY
    from .sampling import flatten_registry_object, normalize_row

    requested = list(organisation_numbers)

    def one(org: str) -> dict[str, Any]:
        result = fetch_json(BRREG_ENTITY.format(org=org), attempts=3)
        if result.status == 200 and isinstance(result.body, dict):
            profile = normalize_row(flatten_registry_object(result.body))
            raw = profile.pop("raw", {})
            profile.pop("csv_row_misaligned", None)
            profile["organisation_number"] = org
            profile["evidence"] = {
                "registry": evidence("registry", "available", "official_registry_live", result.url, value=raw,
                                     retrieved_at=result.retrieved_at, content_sha256=result.content_sha256, source_row_key=org),
            }
            profile["evidence"]["accounting_obligation"] = accounting_obligation_assessment(profile)
            return profile
        state = "not_found" if result.status in {404, 410} else "source_error"
        return {"organisation_number": org, "evidence": {"registry": evidence(
            "registry", state, "official_registry_live", result.url, retrieved_at=result.retrieved_at,
            source_row_key=org, note=result.error or f"HTTP {result.status}")}}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        profiles = list(pool.map(one, requested))
    digest = hashlib.sha256("".join(str((p["evidence"]["registry"].get("content_sha256") or "")) for p in profiles).encode()).hexdigest()
    missing = [p["organisation_number"] for p in profiles if p["evidence"]["registry"]["status"] != "available"]
    return profiles, {
        "registry_snapshot_sha256": digest,
        "registry_source": "live",
        "registry_rows_scanned": len(requested),
        "requested": len(requested),
        "selected": len(requested) - len(missing),
        "absent_from_snapshot": missing,
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "source_error":
        return "source_error"
    return "submission_error"


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"
    return {
        "run_id": run_id,
        "organisation_number": profile["organisation_number"],
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "profile": profile,
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
