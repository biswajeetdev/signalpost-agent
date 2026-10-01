#!/usr/bin/env python3
"""Signalpost evaluator command: organisation numbers in, exactly one contract envelope per input out.

Runs in chunks (--chunk-size, default 50), checkpointing to --checkpoint-dir after every chunk so a
late crash loses at most the in-flight chunk; rerunning the same command resumes from checkpoints
whose fingerprint still matches the inputs.

Example:
  uv run python scripts/run_signalpost.py --organisations batch.txt --bulk brreg-enheter.csv.gz \
    --cache cache/official.sqlite --output out/envelopes.jsonl --profiles-output out/profiles.jsonl \
    --report out/report.json --run-id daily-2026-09-14 --expected-count 100
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import warnings  # noqa: E402

from bs4 import XMLParsedAsHTMLWarning  # noqa: E402

# Some company sites serve XML as text/html; parsing it leniently is intended, the warning is noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from norway_company_agent.batch import profiles_from_bulk, profiles_from_live_registry, read_organisation_inputs  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.cached_official import open_official_cache  # noqa: E402
from norway_company_agent.chunked import run_chunked  # noqa: E402
from norway_company_agent.jobs_nav import NavJobIndex  # noqa: E402
from norway_company_agent.history_prefetch import HistoryPrefetcher  # noqa: E402
from norway_company_agent.pipeline import batch_report, budgeted_official_fetcher, finalize_deferred  # noqa: E402
from norway_company_agent.viewer import render  # noqa: E402
from norway_company_agent.pipeline import RunSettings  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def read_previous(path: str | None) -> dict[str, dict]:
    if not path or not Path(path).exists():
        return {}
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["organisation_number"]: row for row in rows}


def build_jobs_index(budget: RequestBudget, lookback_days: int) -> NavJobIndex:
    """Read the NAV feed once per run, in the background; any failure leaves a failed index, never a failed run."""
    return NavJobIndex(lookback_days=lookback_days, spend=lambda purpose: budget.spend("_run", purpose)).build_in_background()


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"SIGNALPOST_{name}") or default


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost company research batch")
    parser.add_argument("--organisations", "--organizations", "--input", "--companies", "--batch",
                        default=env("ORGANISATIONS"), help="Company batch: JSON, JSONL, CSV or text, gzip or not")
    parser.add_argument("--bulk", "--registry", default=env("BULK"),
                        help="Frozen registry snapshot (Brønnøysund CSV/JSON or flat company list, gzip or not)")
    parser.add_argument("--cache", "--cache-dir", default=env("CACHE"),
                        help="Optional declared cache (file or directory). Missing or empty: live official endpoints")
    parser.add_argument("--output", default=env("OUTPUT", "out/envelopes.jsonl"), help="Envelope JSONL")
    parser.add_argument("--profiles-output", default=env("PROFILES_OUTPUT"), help="Profile JSONL, the snapshot the next refresh diffs against")
    parser.add_argument("--report", default=env("REPORT"))
    parser.add_argument("--run-id", default=env("RUN_ID"))
    parser.add_argument("--expected-count", type=int, default=int(env("EXPECTED_COUNT", "0")), help="0: take the batch size")
    parser.add_argument("--previous-profiles", default=env("PREVIOUS_PROFILES"), help="Profiles JSONL from the previous run, for change detection")
    parser.add_argument("--max-requests", type=int, default=int(env("MAX_REQUESTS", "0")), help="0: 20 per company")
    parser.add_argument("--max-minutes", type=float, default=float(env("MAX_MINUTES", "45")))
    parser.add_argument("--workers", type=int, default=int(env("WORKERS", "16")))
    parser.add_argument("--discovery-allowance", type=int, default=32)
    parser.add_argument("--company-seconds", type=float, default=float(env("COMPANY_SECONDS", "90")),
                        help="Wall-clock cap on one company's website requests (0 disables)")
    parser.add_argument("--disable-unique-name-rule", action="store_true")
    parser.add_argument("--chunk-size", "--checkpoint-every", type=int, default=100, help="Organisations per checkpointed chunk")
    parser.add_argument("--chunk-retries", type=int, default=1, help="Extra attempts for a chunk before it falls back to failed envelopes")
    parser.add_argument("--checkpoint-dir", help="Default: <report>.checkpoints/ next to --report")
    parser.add_argument("--resume", action="store_true", help="Accepted for the evaluator contract; rerunning the same --run-id always resumes")
    parser.add_argument("--modules", help="Accepted for the evaluator contract; every module always runs")
    parser.add_argument("--jobs-lookback-days", type=int, default=int(env("JOBS_LOOKBACK_DAYS", "120")),
                        help="How far back the NAV public job feed is read for still-active ads")
    parser.add_argument("--no-jobs", action="store_true", help="Skip the NAV job-feed connector")
    parser.add_argument("organisations_positional", nargs="?", help=argparse.SUPPRESS)
    args, unknown = parser.parse_known_args()
    if unknown:
        print(f"warning: ignoring unrecognised arguments: {' '.join(unknown)}", file=sys.stderr)
    args.organisations = args.organisations or args.organisations_positional
    if not args.organisations:
        raise SystemExit("A company batch is required: --organisations <file> (or SIGNALPOST_ORGANISATIONS)")
    output = Path(args.output)
    args.profiles_output = args.profiles_output or str(output.with_name(output.stem + ".profiles.jsonl"))
    args.report = args.report or str(output.with_name(output.stem + ".report.json"))
    args.run_id = args.run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if args.chunk_size < 1:
        raise SystemExit(f"--chunk-size must be >= 1, got {args.chunk_size}")
    if args.chunk_retries < 0:
        raise SystemExit(f"--chunk-retries must be >= 0, got {args.chunk_retries}")

    inputs = read_organisation_inputs(args.organisations)
    organisations = [item["organisation_number"] for item in inputs]
    args.expected_count = args.expected_count or len(organisations)
    if len(organisations) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(organisations)}")
    # 20 per company, plus room for the one-off NAV feed read (~4 pages per look-back day).
    budget = RequestBudget(args.max_requests or 20 * len(organisations) + 5 * args.jobs_lookback_days, args.max_minutes * 60)
    budget.paced_pending = len(organisations)
    budget.company_seconds = args.company_seconds or None
    jobs_index = None if args.no_jobs else build_jobs_index(budget, args.jobs_lookback_days)
    # The paced filing-history endpoint gets its own thread from second 0 (input order), so workers never queue on it.
    history = HistoryPrefetcher(organisations, budget, budgeted_official_fetcher).start()
    # No bulk supplied: the batch file itself is the registry source when it carries company rows,
    # otherwise the live entity endpoint fills each registry row (see profiles_from_live_registry).
    profiles, registry = profiles_from_bulk(args.bulk, organisations) if args.bulk else profiles_from_live_registry(organisations)
    settings = RunSettings(
        discovery_allowance=args.discovery_allowance,
        unique_name_rule=not args.disable_unique_name_rule,
        workers=args.workers,
    )
    report_path = Path(args.report)
    checkpoint_dir = Path(args.checkpoint_dir) if args.checkpoint_dir else report_path.with_name(report_path.stem + ".checkpoints")
    envelopes, enriched, report = run_chunked(
        profiles,
        cache=open_official_cache(args.cache),
        budget=budget,
        run_id=args.run_id,
        settings=settings,
        previous=read_previous(args.previous_profiles),
        registry_sha256=registry["registry_snapshot_sha256"],
        checkpoint_dir=checkpoint_dir,
        chunk_size=args.chunk_size,
        chunk_retries=args.chunk_retries,
        jobs_index=jobs_index,
        history_prefetch=history,
    )
    rebuilt = finalize_deferred(envelopes, enriched, budget=budget, history_prefetch=history, jobs_index=jobs_index)
    if any(rebuilt.values()):
        # Envelopes changed after the chunks: recompute the envelope-derived report parts.
        refreshed = batch_report(envelopes, budget, report["started_at"], report["completed_at"])
        for key in ("module_states", "available_claims", "validation", "unique_organisations"):
            report[key] = refreshed[key]
    report["history_stream"] = {"fetched": len(history.results), "of": len(organisations), "filled_after_batch": rebuilt["history"]}
    if jobs_index is not None:
        report["jobs_feed"] = {"state": jobs_index.state, "pages": jobs_index.pages, "active_ads": jobs_index.active_ads,
                               "lookback_days": jobs_index.lookback_days, "deferred_then_filled": rebuilt["jobs"], "note": jobs_index.note}
    report = {"run_id": args.run_id, "expected_count": args.expected_count, "registry": registry, **report}
    report["validation"]["exact_expected_count"] = len(envelopes) == args.expected_count
    report["validation"]["input_order"] = [item["organisation_number"] for item in envelopes] == organisations
    report["validation"]["passed"] = (
        report["validation"]["passed"]
        and report["validation"]["exact_expected_count"]
        and report["validation"]["input_order"]
        and report["unique_organisations"]
    )
    write_jsonl(Path(args.profiles_output), enriched)
    write_jsonl(Path(args.output), envelopes)
    try:  # human-readable companion; never affects the run's exit status
        html_path = output.with_suffix(".html")
        html_path.write_text(render(envelopes, run_id=args.run_id, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds")), encoding="utf-8")
        report["viewer"] = str(html_path)
    except Exception as exc:
        print(f"warning: viewer not written: {exc}", file=sys.stderr)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("run_id", "envelopes", "module_states", "operations", "validation")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["validation"]["passed"] else 1)


if __name__ == "__main__":
    main()
