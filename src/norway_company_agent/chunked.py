"""Fail-safe chunked runner: divide -> run chunk -> check -> checkpoint -> retry/fallback -> merge.

Chunks run sequentially, in one process, against one shared RequestBudget and RobotsCache (the
Brønnøysund pacing lock and the per-run request/wall-clock ceiling are both in-process state that a
single run_batch call already handles correctly across chunks). A checkpoint is written atomically
right after each chunk finishes, so a crash loses at most the in-flight chunk. On resume, a checkpoint
is reused only if its fingerprint still matches the chunk's inputs (run_id included: resume only
continues the SAME interrupted run) and its envelopes still validate; everything else runs (or
re-runs) in order, in input order for the final merge.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeVar

from .budget import RequestBudget, RobotsCache
from .cached_official import OfficialCache
from .contract import build_envelope, validate_envelope
from .evidence import utc_now
from .pipeline import RunSettings, _mark_failed, batch_report, run_batch

CHECKPOINT_VERSION = 1
T = TypeVar("T")


def divide(items: Sequence[T], size: int) -> list[list[T]]:
    if size < 1:
        raise ValueError(f"chunk size must be >= 1, got {size}")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def chunk_fingerprint(
    organisations: list[str],
    *,
    run_id: str,
    registry_sha256: str,
    previous: Mapping[str, Mapping[str, Any]],
    cache: OfficialCache,
    settings: RunSettings,
) -> str:
    """Identifies everything that can change this chunk's output, or that must not be reused across runs.

    `run_id` is included on purpose: resume is only meant to continue the SAME interrupted run (same
    command, same --run-id). Without it, a later run against the same organisations/snapshot/previous
    profiles (e.g. a refresh evaluation the next day) would silently reuse stale live checkpoint data
    instead of re-crawling. `workers` is excluded — it doesn't affect output.
    """
    payload = {
        "version": CHECKPOINT_VERSION,
        "run_id": run_id,
        "organisations": organisations,
        "registry_snapshot_sha256": registry_sha256,
        "previous": hashlib.sha256(
            json.dumps([previous.get(org) for org in organisations], sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "cache_snapshots": {name: snapshot["sha256"] for name, snapshot in cache.snapshots.items()},
        "settings": {
            "discovery_allowance": settings.discovery_allowance,
            "max_hosts": settings.max_hosts,
            "unique_name_rule": settings.unique_name_rule,
            "min_seconds_for_discovery": settings.min_seconds_for_discovery,
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def check_chunk(envelopes: list[dict[str, Any]], profiles: list[dict[str, Any]], organisations: list[str]) -> list[str]:
    """Hard-gate problems in one chunk's output; an empty list means it is safe to checkpoint and merge."""
    problems = []
    if len(envelopes) != len(organisations):
        problems.append(f"expected {len(organisations)} envelopes, got {len(envelopes)}")
    if len(profiles) != len(organisations):
        problems.append(f"expected {len(organisations)} profiles, got {len(profiles)}")
    envelope_orgs = [item.get("organisation_number") for item in envelopes]
    if envelope_orgs != organisations:
        problems.append("envelope organisation order does not match the chunk's input order")
    profile_orgs = [item.get("organisation_number") for item in profiles]
    if profile_orgs != organisations:
        problems.append("profile organisation order does not match the chunk's input order")
    if len(set(envelope_orgs)) != len(envelope_orgs):
        problems.append("duplicate organisation numbers among envelopes")
    for item in envelopes:
        problems.extend(f"{item.get('organisation_number')}: {problem}" for problem in validate_envelope(item))
    return problems


def fallback_chunk(
    profiles: list[dict[str, Any]],
    error: str,
    *,
    budget: RequestBudget,
    run_id: str,
    started_at: str,
    completed_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persistently-failing chunk -> failed terminal envelopes, never dropped. changes=[] since diffing
    a failed profile against `previous` would report fake material changes."""
    envelopes, failed_profiles = [], []
    for original in profiles:
        org = original["organisation_number"]
        operations = {"requests": budget.by_company[org], "runtime_ms": 0, "third_party_cost_usd": 0}
        profile = _mark_failed(deepcopy(original), RuntimeError(error))
        envelope = build_envelope(profile, run_id=run_id, started_at=started_at, completed_at=completed_at, operations=operations, changes=[])
        if validate_envelope(envelope):
            profile = _mark_failed({"organisation_number": org, "name": original.get("name")}, RuntimeError(error))
            envelope = build_envelope(profile, run_id=run_id, started_at=started_at, completed_at=completed_at, operations=operations, changes=[])
        envelopes.append(envelope)
        failed_profiles.append(profile)
    return envelopes, failed_profiles


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _checkpoint_matches_run(payload: dict[str, Any], run_id: str) -> bool:
    """True only if every envelope carries this run's id and the payload has the shape run_chunked
    and batch_report expect. Raises KeyError/TypeError/AttributeError on anything malformed or
    tampered with; the caller treats that the same as False (checkpoint not reusable)."""
    envelopes, profiles = payload["envelopes"], payload["profiles"]
    if not isinstance(envelopes, list) or not isinstance(profiles, list) or not all(isinstance(item, dict) for item in profiles):
        return False
    for envelope in envelopes:
        if not isinstance(envelope["run"], dict) or envelope["run"]["run_id"] != run_id:
            return False
        if not isinstance(envelope["modules"], dict) or not isinstance(envelope["claims"], list):
            return False
        operations = envelope["operations"]
        if not isinstance(operations, dict) or not _is_int(operations["requests"]) or not _is_int(operations["runtime_ms"]):
            return False
    operations = payload["operations"]
    if not isinstance(operations, dict) or not _is_int(operations["requests"]) or not isinstance(operations["by_purpose"], dict):
        return False
    if isinstance(payload["elapsed_seconds"], bool) or not isinstance(payload["elapsed_seconds"], (int, float)):
        return False
    return isinstance(payload["started_at"], str) and isinstance(payload["completed_at"], str)


def load_checkpoint(path: Path, fingerprint: str, organisations: list[str], run_id: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CHECKPOINT_VERSION or payload.get("fingerprint") != fingerprint:
        return None
    if payload.get("outcome") == "fallback":  # failed chunks are always retried on resume
        return None
    try:
        if not _checkpoint_matches_run(payload, run_id):
            return None
        if check_chunk(payload["envelopes"], payload["profiles"], organisations):
            return None
    except (KeyError, TypeError, AttributeError):
        return None
    return payload


@dataclass
class _Chunk:
    index: int
    profiles: list[dict[str, Any]]
    organisations: list[str]
    fingerprint: str
    path: Path
    loaded: dict[str, Any] | None


def run_chunked(
    profiles: list[dict[str, Any]],
    *,
    cache: OfficialCache,
    budget: RequestBudget,
    run_id: str,
    settings: RunSettings,
    previous: Mapping[str, Mapping[str, Any]],
    registry_sha256: str,
    checkpoint_dir: Path,
    chunk_size: int = 50,
    chunk_retries: int = 1,
    **enrich_overrides: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Enrich every profile chunk by chunk, checkpointing after each, and return (envelopes, profiles,
    report) merged in input order — the same shape run_batch returns."""
    plan = []
    for index, chunk in enumerate(divide(profiles, chunk_size)):
        organisations = [item["organisation_number"] for item in chunk]
        fingerprint = chunk_fingerprint(organisations, run_id=run_id, registry_sha256=registry_sha256, previous=previous, cache=cache, settings=settings)
        path = checkpoint_dir / f"chunk-{index:04d}.json"
        plan.append(_Chunk(index, chunk, organisations, fingerprint, path, load_checkpoint(path, fingerprint, organisations, run_id)))

    # Resumed chunks spent real requests and wall-clock time in an earlier process; count that against
    # this run's locked budget before any new chunk runs.
    resumed_requests = sum(item.loaded["operations"]["requests"] for item in plan if item.loaded)
    budget.charge_prior(resumed_requests, sum(item.loaded["elapsed_seconds"] for item in plan if item.loaded))

    robots = RobotsCache()
    all_envelopes: list[dict[str, Any]] = []
    all_profiles: list[dict[str, Any]] = []
    chunk_operations: list[dict[str, Any]] = []
    chunk_starts: list[str] = []
    chunk_completions: list[str] = []
    stats: Counter[str] = Counter()
    fallback_organisations: list[str] = []

    for item in plan:
        if item.loaded is not None:
            all_envelopes.extend(item.loaded["envelopes"])
            all_profiles.extend(item.loaded["profiles"])
            chunk_operations.append(item.loaded["operations"])
            chunk_starts.append(item.loaded["started_at"])
            chunk_completions.append(item.loaded["completed_at"])
            stats["resumed"] += 1
            continue

        before = Counter(budget.by_purpose)
        t0 = time.monotonic()
        started_at = utc_now()
        attempts_allowed = 1 + chunk_retries
        attempt = 0
        envelopes: list[dict[str, Any]] = []
        enriched: list[dict[str, Any]] = []
        problems: list[str] = []
        while True:
            attempt += 1
            try:
                envelopes, enriched, _ = run_batch(
                    deepcopy(item.profiles), cache=cache, budget=budget, run_id=run_id, settings=settings,
                    previous=previous, robots=robots, **enrich_overrides,
                )
                problems = check_chunk(envelopes, enriched, item.organisations)
            except Exception as exc:  # every chunk still ends in a checkpoint, never an unhandled crash
                envelopes, enriched = [], []
                problems = [f"{type(exc).__name__}: {str(exc)[:200]}"]
            if not problems:
                break
            can_retry = attempt < attempts_allowed and budget.seconds_left() > settings.min_seconds_for_discovery and budget.remaining() > budget.reserve
            if not can_retry:
                break
        completed_at = utc_now()

        if problems:
            envelopes, enriched = fallback_chunk(item.profiles, problems[0], budget=budget, run_id=run_id, started_at=started_at, completed_at=completed_at)
            outcome = "fallback"
            fallback_organisations.extend(item.organisations)
        else:
            outcome = "passed" if attempt == 1 else "retried"

        operations = {"requests": sum(env["operations"]["requests"] for env in envelopes), "by_purpose": dict(Counter(budget.by_purpose) - before)}
        payload = {
            "version": CHECKPOINT_VERSION, "fingerprint": item.fingerprint, "index": item.index, "organisations": item.organisations,
            "outcome": outcome, "attempts": attempt, "problems": problems, "started_at": started_at, "completed_at": completed_at,
            "elapsed_seconds": time.monotonic() - t0, "envelopes": envelopes, "profiles": enriched, "operations": operations,
        }
        write_checkpoint(item.path, payload)

        all_envelopes.extend(envelopes)
        all_profiles.extend(enriched)
        chunk_operations.append(operations)
        chunk_starts.append(started_at)
        chunk_completions.append(completed_at)
        stats[outcome] += 1

    started_at = min(chunk_starts) if chunk_starts else utc_now()
    completed_at = max(chunk_completions) if chunk_completions else utc_now()
    report = batch_report(all_envelopes, budget, started_at, completed_at)
    counts = sorted(env["operations"]["requests"] for env in all_envelopes)
    by_purpose: Counter[str] = Counter()
    for operations in chunk_operations:
        by_purpose.update(operations["by_purpose"])
    report["operations"].update({
        "requests": sum(env["operations"]["requests"] for env in all_envelopes),
        "by_purpose": dict(by_purpose),
        "per_company_p50": counts[len(counts) // 2] if counts else 0,
        "per_company_max": counts[-1] if counts else 0,
        "resumed_requests": resumed_requests,
    })
    report["chunks"] = {
        "size": chunk_size,
        "total": len(plan),
        "passed_first_try": stats["passed"],
        "retried": stats["retried"],
        "fallback": stats["fallback"],
        "resumed": stats["resumed"],
        "fallback_organisations": fallback_organisations,
    }
    return all_envelopes, all_profiles, report
