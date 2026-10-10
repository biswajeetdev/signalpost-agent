"""Coverage scorecard for one or more runs: companies with each external field family, claim counts,
failures and guard-rail warnings. Compares runs side by side when several are given.

    uv run python scripts/score_run.py out/base out/v8      # directories holding envelopes.jsonl + report.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

FAMILIES = ("official_website", "social_profile", "hiring_signal", "dated_news")


def _seconds(started: str | None, completed: str | None) -> int | None:
    if not started or not completed:
        return None
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: E731
    return round((parse(completed) - parse(started)).total_seconds())


def score(run_dir: Path) -> dict:
    companies = {family: 0 for family in FAMILIES}
    claims = {family: 0 for family in FAMILIES}
    envelopes = failed = watchdog = 0
    for line in (run_dir / "envelopes.jsonl").open():
        envelope = json.loads(line)
        envelopes += 1
        watchdog += bool((envelope.get("operations") or {}).get("watchdog"))
        failed += any(state == "failed" for module, state in (envelope.get("modules") or {}).items() if module in {"registry", "financials", "roles"})
        for family in FAMILIES:
            available = [claim for claim in envelope.get("claims") or [] if claim.get("field") == family and claim.get("availability") == "available"]
            companies[family] += bool(available)
            claims[family] += len(available)
    report = json.loads((run_dir / "report.json").read_text())
    guard = report.get("guardrails") or {}
    return {
        "run": run_dir.name,
        "envelopes": envelopes,
        "valid": (report.get("validation") or {}).get("passed"),
        "contract": guard.get("contract_passed"),
        "watchdog": watchdog,
        "official_failed": failed,
        "requests": (report.get("operations") or {}).get("requests"),
        "seconds": _seconds(report.get("started_at"), report.get("completed_at")),
        **{f"{family}.companies": companies[family] for family in FAMILIES},
        **{f"{family}.claims": claims[family] for family in FAMILIES},
        "warnings": guard.get("warnings") or [],
    }


def main(paths: list[str]) -> None:
    rows = [score(Path(path)) for path in paths]
    keys = [key for key in rows[0] if key != "warnings"]
    width = max(len(key) for key in keys)
    print(" " * width + "".join(f"{row['run']:>16}" for row in rows))
    for key in keys[1:]:
        print(f"{key:<{width}}" + "".join(f"{str(row[key]):>16}" for row in rows))
    for row in rows:
        for warning in row["warnings"]:
            print(f"[{row['run']}] warning: {warning}")


if __name__ == "__main__":
    main(sys.argv[1:])
