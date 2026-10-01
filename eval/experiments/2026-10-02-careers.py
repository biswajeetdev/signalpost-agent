#!/usr/bin/env python3
"""Careers-page postings on the verified websites of a previous run: coverage and every posting found.

  uv run python eval/experiments/2026-10-02-careers.py --profiles <run profiles.jsonl>
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import RequestBudget, RobotsCache  # noqa: E402
from norway_company_agent.pipeline import careers_record  # noqa: E402
from norway_company_agent.site_discovery import make_site_fetchers  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    sites = [row for row in rows if row["evidence"]["website"]["status"] == "available"]
    budget, robots = RequestBudget(10 * len(sites), 3600), RobotsCache()

    def run(row: dict) -> tuple[dict, dict]:
        fetch, allowed = make_site_fetchers(budget, row["organisation_number"], allowance=8, robots=robots)
        home = fetch(row["evidence"]["website"]["value"]["final_url"])
        return row, careers_record(home if home.status == 200 and home.html else None, fetch, allowed)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run, sites))
    hits = [(row, record) for row, record in results if record["status"] == "available"]
    print(json.dumps({"verified_sites": len(sites), "companies_with_postings": len(hits),
                      "postings": sum(len(record["value"]["ads"]) for _, record in hits), "requests": budget.used}))
    for row, record in hits:
        print(f"## {row.get('name')} | {row['evidence']['website']['value']['final_url']}")
        for ad in record["value"]["ads"][:6]:
            print(f"   {ad['extraction']:24} | {ad['title'][:60]:60} | {ad['url'][:90]}")


if __name__ == "__main__":
    main()
