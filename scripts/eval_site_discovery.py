#!/usr/bin/env python3
"""Live website-discovery evaluation against the Builderr 100-profile sample.

Builderr's published domain is a reference, not ground truth: a published domain that differs
is reported for manual review with the proof pages that justified it.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import RequestBudget, RobotsCache  # noqa: E402
from norway_company_agent.site_discovery import discover_website, make_site_fetchers  # noqa: E402


class SqliteCounts:
    """Read-only Mapping-style lookup of share counts from the official cache."""

    def __init__(self, path: Path, table: str, key: str) -> None:
        self.path, self.table, self.key = path, table, key

    def get(self, value: str, default: int = 0) -> int:
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            row = db.execute(f"SELECT entities FROM {self.table} WHERE {self.key} = ?", (value,)).fetchone()
        return int(row[0]) if row else default


def registry_rows(bulk: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    rows = {}
    with gzip.open(bulk, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["organisasjonsnummer"] in wanted:
                rows[row["organisasjonsnummer"]] = row
                if len(rows) == len(wanted):
                    break
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", default=str(ROOT / "eval/gold/builderr-sample-100.json"))
    parser.add_argument("--bulk", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--split", default=None, help="development | validation | held_out (default: all)")
    parser.add_argument("--allowance", type=int, default=14)
    parser.add_argument("--max-hosts", type=int, default=4)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--unique-name-rule", action="store_true", help="Enable the unique full-legal-name domain rule")
    args = parser.parse_args()

    gold = [row for row in json.loads(Path(args.gold).read_text(encoding="utf-8")) if not args.split or row.get("split") == args.split]
    rows = registry_rows(Path(args.bulk), {row["org"] for row in gold})
    cache = Path(args.cache)
    shared_domains = SqliteCounts(cache, "email_domains", "domain")
    shared_phones = SqliteCounts(cache, "phones", "phone")
    name_keys = SqliteCounts(cache, "name_keys", "key") if args.unique_name_rule else None
    budget = RequestBudget(max_requests=len(gold) * 20, reserve_fraction=0)
    robots = RobotsCache()
    started = time.monotonic()

    def run(reference: dict) -> dict:
        org = reference["org"]
        fetch, robots_allowed = make_site_fetchers(budget, org, allowance=args.allowance, robots=robots)
        record, _ = discover_website(rows[org], shared_domains=shared_domains, shared_phones=shared_phones, fetch=fetch, robots_allowed=robots_allowed, max_hosts=args.max_hosts, name_keys=name_keys)
        reference_web = (reference.get("web") or {}).get("value") or {}
        return {
            "organisation_number": org,
            "name": reference["name"],
            "split": reference.get("split"),
            "reference_domain": reference_web.get("registered_domain"),
            "reference_identity": (reference_web.get("identity_assessment") or {}).get("status"),
            "requests": budget.by_company[org],
            "record": record,
        }

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run, gold))
    elapsed = time.monotonic() - started

    outcome: Counter[str] = Counter()
    review = []
    for item in results:
        record = item["record"]
        domain = (record.get("value") or {}).get("registered_domain")
        reference = item["reference_domain"]
        if record["status"] != "available":
            key = f"{record['status']}_reference_exists" if reference else record["status"]
        elif not reference:
            key = "published_no_reference"
        elif domain == reference or domain.split(".")[0] == reference.split(".")[0]:
            key = "published_matches_reference"
        else:
            key = "published_differs_review"
            review.append({"name": item["name"], "published": domain, "reference": reference, "proofs": record["value"]["identity_assessment"]["proofs"], "proof_pages": [page["url"] for page in record["value"]["proof_pages"]]})
        outcome[key] += 1
    per_company = sorted(item["requests"] for item in results)
    summary = {
        "companies": len(results),
        "outcomes": dict(outcome),
        "published": sum(1 for item in results if item["record"]["status"] == "available"),
        "reference_websites": sum(1 for item in results if item["reference_domain"]),
        "requests": budget.report(),
        "requests_p95": per_company[int(len(per_company) * 0.95)] if per_company else 0,
        "wall_seconds": round(elapsed, 1),
        "candidate_source_of_published": dict(Counter(item["record"]["value"]["candidate_source"] for item in results if item["record"]["status"] == "available")),
        "differs_for_review": review,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
