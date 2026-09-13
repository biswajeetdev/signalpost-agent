# Signalpost learning harness

The winning system should improve through measured experiments, not through vague self-reflection. A simple evaluation loop is enough:

`try a strategy → preserve the attempt → score it → compare it → promote or reject it`

The public development set is training ground. Hidden companies are the exam.

## 1. Create a strategy registry

Give every discovery or extraction route a stable name and version. Start with:

- registry-provided website
- sitemap and robots discovery
- static homepage crawl
- targeted `/about`, `/contact`, `/leadership`, `/locations`, `/careers` and `/news` paths
- JSON-LD and OpenGraph extraction
- search-provider candidate discovery
- leader/founder bridge from official role to public brand
- browser-rendered fallback for a confirmed JavaScript shell
- annual-account PDF fallback

Do not let the agent silently invent and deploy new production code. New strategies enter the registry, run on the development set and face the same tests as the current winner.

## 2. Save every attempt

For every company and strategy, record:

- strategy name and version
- input organisation number
- requested URLs and redirect chain
- raw snapshot hashes
- candidate domains, profiles and claims
- accepted and rejected claims with reasons
- exact-identity evidence
- runtime, request count and cost
- errors and availability states

This makes failures useful. You can see whether a strategy found nothing, chose the wrong company, extracted bad data or simply cost too much.

## 3. Score in the right order

Use a hand-reviewed public gold set. Score each strategy in this order:

1. Wrong-company publications — must not increase
2. Supported-claim precision — must not fall
3. Evidence validity — every accepted claim points to the right source span
4. Coverage and recall — useful supported fields added
5. Refresh correctness — real changes found without false changes
6. Runtime, requests and cost

A strategy that finds more data about the wrong company loses.

## 4. Use a strict promotion rule

Promote a challenger only when all are true:

- zero new material wrong-company publications
- no meaningful drop in claim precision
- evidence completeness remains 100% for published material claims
- useful coverage or recall improves by a declared minimum
- runtime and cost remain within budget

Keep the previous strategy available for rollback. Store the decision and the exact evaluation report.

For the first version, use a decision table rather than reinforcement learning. Example: static HTML first; browser rendering only after a JavaScript-shell test; PDF layout parsing only when plain text fails. A contextual bandit or learned router is optional later, after enough labelled attempts exist.

## 5. Separate learning from refresh

Learning chooses better strategies. Refresh revisits evidence with the chosen frozen strategies.

A refresh should preserve the previous value, store the new snapshot and emit a typed change such as `new_role`, `closed_job`, `new_location`, `new_filing` or `changed_description`. A failed refresh must not erase the last supported value.

## 6. Freeze before daily evaluation

Before Builderr selects the next random daily batch, freeze:

- code and dependency lockfile
- strategy versions and routing table
- prompts, models and model versions
- thresholds and publication gates
- source allowlist and budgets

Operational retries may follow the frozen policy. Hidden scores and labels must not tune the running submission.

## Recommended starter repositories

### Core crawl and extraction

- [Scrapy](https://github.com/scrapy/scrapy) — queues, throttling, retries, deduplication and crawl budgets
- [scrapy-playwright](https://github.com/scrapy-plugins/scrapy-playwright) — browser fallback for pages that truly require JavaScript
- [extruct](https://github.com/scrapinghub/extruct) — JSON-LD, microdata, RDFa and OpenGraph extraction
- [Trafilatura](https://github.com/adbar/trafilatura) — readable text and metadata extraction
- [Pydantic](https://github.com/pydantic/pydantic) — typed company, claim and evidence envelopes

### Identity and normalization

- [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) — fuzzy candidate matching after organisation-number checks
- [tldextract](https://github.com/john-kurkowski/tldextract) — registered-domain normalization
- [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers) — phone normalization and corroboration

### Documents and difficult pages

- [pypdf](https://github.com/py-pdf/pypdf) — fast first pass for text PDFs
- [Docling](https://github.com/docling-project/docling) — tables and layout-heavy reports when the simple pass fails
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) — scanned pages only

### Evaluation and storage

- [pytest](https://github.com/pytest-dev/pytest) — regression and connector tests
- [DuckDB](https://github.com/duckdb/duckdb) with Parquet — local attempt analysis and benchmark reports
- [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) — runtime traces; keep claim provenance in product tables

### Optional connector experiments

- [JobSpy](https://github.com/speedyapply/JobSpy) can help benchmark job discovery across several platforms.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) can help test public video metadata discovery.

These are experiments, not automatic permission to collect. The submitted access method must comply with source terms and applicable law. Prefer official or licensed APIs, and independently verify every published company claim.

## Smallest useful repository layout

```text
strategies/
  registry_site.py
  sitemap_static.py
  search_candidates.py
  browser_fallback.py
  pdf_fallback.py
eval/
  gold_companies.jsonl
  score_attempts.py
  promotion_gate.py
snapshots/
claims/
reports/
```

One command should run the public loop and produce a comparison report:

```bash
python -m eval.run --corpus eval/gold_companies.jsonl --challenger browser_fallback_v2
```
