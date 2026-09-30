#!/usr/bin/env python3
"""Website-discovery A/B: re-run discovery only on the registry rows of a previous run's profiles and
compare states with that run. Prints counts, every flip, and the proof for each newly published site.

  uv run python eval/experiments/2026-10-01-website-v4.py --profiles <prev profiles.jsonl> --output <out.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import RequestBudget, RobotsCache  # noqa: E402
from norway_company_agent.cached_official import open_official_cache  # noqa: E402
from norway_company_agent.site_discovery import discover_website, make_site_fetchers  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--allowance", type=int, default=24)
    parser.add_argument("--only", choices=("all", "unpublished"), default="all")
    args = parser.parse_args()

    rows = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only == "unpublished":
        rows = [row for row in rows if row["evidence"]["website"]["status"] != "available"]
    cache = open_official_cache(None)
    shared = {"domains": cache.shared_domains(), "phones": cache.shared_phones(), "names": cache.name_keys()}
    budget = RequestBudget(40 * len(rows), 3 * 3600)
    robots = RobotsCache()

    def run(row: dict) -> tuple[str, dict]:
        org = row["organisation_number"]
        registry = (row["evidence"].get("registry") or {}).get("value") or {}
        fetch, allowed = make_site_fetchers(budget, org, allowance=args.allowance, robots=robots)
        record, _ = discover_website(registry, shared_domains=shared["domains"], shared_phones=shared["phones"],
                                     fetch=fetch, robots_allowed=allowed, name_keys=shared["names"])
        return org, record

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = dict(pool.map(run, rows))
    elapsed = time.monotonic() - started

    before = Counter(row["evidence"]["website"]["status"] for row in rows)
    after = Counter(record["status"] for record in results.values())
    print(json.dumps({"companies": len(rows), "before": before, "after": after, "requests": budget.used, "seconds": round(elapsed)}))
    with open(args.output, "w", encoding="utf-8") as handle:
        for row in rows:
            org = row["organisation_number"]
            old, new = row["evidence"]["website"], results[org]
            handle.write(json.dumps({"organisation_number": org, "name": row.get("name"), "before": old["status"], "after": new["status"], "record": new}, ensure_ascii=False) + "\n")
            if old["status"] != new["status"]:
                value = new.get("value") or {}
                proofs = (value.get("identity_assessment") or {}).get("proofs")
                spans = [(page.get("url"), sorted((page.get("claim_spans") or {}).items())) for page in value.get("proof_pages") or [] if page.get("claim_spans")]
                print(f"{row.get('name')} | {old['status']} -> {new['status']} | {value.get('final_url')} | {proofs}")
                if new["status"] == "available":
                    for url, items in spans:
                        for proof, span in items:
                            print(f"      {proof} @ {url}: {span[:150]}")


if __name__ == "__main__":
    main()
