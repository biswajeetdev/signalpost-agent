#!/usr/bin/env python3
"""Signalpost evaluator command: organisation numbers in, exactly one contract envelope per input out.

Example:
  uv run python scripts/run_signalpost.py --organisations batch.txt --bulk brreg-enheter.csv.gz \
    --cache cache/official.sqlite --output out/envelopes.jsonl --profiles-output out/profiles.jsonl \
    --report out/report.json --run-id daily-2026-09-14 --expected-count 100
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profiles_from_bulk, read_organisation_inputs  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.cached_official import OfficialCache  # noqa: E402
from norway_company_agent.pipeline import RunSettings, run_batch  # noqa: E402


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost company research batch")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL or text list of organisation numbers")
    parser.add_argument("--bulk", required=True, help="Brønnøysund entity bulk snapshot (gzip CSV)")
    parser.add_argument("--cache", required=True, help="Declared official cache from scripts/build_official_cache.py")
    parser.add_argument("--output", required=True, help="Envelope JSONL")
    parser.add_argument("--profiles-output", required=True, help="Profile JSONL, the snapshot the next refresh diffs against")
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--previous-profiles", help="Profiles JSONL from the previous run, for change detection")
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--max-minutes", type=float, default=45.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--discovery-allowance", type=int, default=14)
    parser.add_argument("--disable-unique-name-rule", action="store_true")
    args = parser.parse_args()

    inputs = read_organisation_inputs(args.organisations)
    organisations = [item["organisation_number"] for item in inputs]
    if len(organisations) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(organisations)}")
    budget = RequestBudget(args.max_requests, args.max_minutes * 60)
    profiles, registry = profiles_from_bulk(args.bulk, organisations)
    settings = RunSettings(
        discovery_allowance=args.discovery_allowance,
        unique_name_rule=not args.disable_unique_name_rule,
        workers=args.workers,
    )
    envelopes, enriched, report = run_batch(
        profiles,
        cache=OfficialCache(args.cache),
        budget=budget,
        run_id=args.run_id,
        settings=settings,
        previous=read_previous(args.previous_profiles),
    )
    report = {"run_id": args.run_id, "expected_count": args.expected_count, "registry": registry, **report}
    report["validation"]["exact_expected_count"] = len(envelopes) == args.expected_count
    report["validation"]["passed"] = report["validation"]["passed"] and report["validation"]["exact_expected_count"] and report["unique_organisations"]
    write_jsonl(Path(args.profiles_output), enriched)
    write_jsonl(Path(args.output), envelopes)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("run_id", "envelopes", "module_states", "operations", "validation")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["validation"]["passed"] else 1)


if __name__ == "__main__":
    main()
