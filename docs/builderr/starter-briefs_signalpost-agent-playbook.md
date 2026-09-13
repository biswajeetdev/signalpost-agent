# Signalpost agent playbook

This is the reference architecture, not a mandated framework. Keep any component only when a blind evaluation shows that it improves coverage without increasing wrong-company or unsupported-claim rates.

## 1. Start with authoritative identity

Seed every run with the organisation number, legal name, legal form, registered address, industry and latest filing year from Brønnøysundregistrene.

Treat the organisation number as the stable key. A brand, domain or social handle is only a candidate until it is tied back to the exact entity.

## 2. Resolve the public identity

Build an evidence graph rather than trusting name similarity:

`legal entity → official site candidate → public brand/aliases → leaders/founders → company profile candidate → reverse proof`

Useful proof includes the organisation number on the site, or a strong combination of legal name, address, phone, leadership and independent official evidence. Parent brands, franchises, sister companies and portfolio pages are not exact matches.

Leaders can help bridge a legal name to a public brand: find a verified role in the official register, locate the person's public profile through a permitted source, then confirm that the stated company resolves back to the same entity. This generates candidates; it does not replace exact-entity verification.

## 3. Crawl deterministically first

A practical baseline:

- Scrapy for queues, throttling, retry, deduplication and per-domain budgets
- Sitemap and static HTML before browser rendering
- extruct for JSON-LD, OpenGraph and microdata
- Trafilatura for readable page text
- Playwright only when a deterministic check identifies a JavaScript shell
- Pydantic or an equivalent typed validator before publication
- Plain PDF text extraction first; layout/OCR only for difficult annual reports

Prioritise `/about`, `/om-oss`, `/contact`, `/kontakt`, `/leadership`, `/ledelse`, `/locations`, `/careers`, `/jobs`, `/news` and `/investor`.

## 4. Use a source ladder

1. Official registers and annual accounts
2. Verified company-owned sites and feeds
3. Official or licensed platform APIs
4. Permitted public pages with recorded terms and provenance
5. Search/discovery providers for candidate generation only

Never use search rank or an unofficial scraper response as the evidence for a published fact. Re-fetch the underlying permitted source and preserve it.

## 5. Preserve evidence before extraction

Store immutable raw snapshots with:

- requested and final URL
- redirect chain and response status
- retrieval timestamp and relevant effective/reporting date
- content hash
- parser and extractor version
- source class and access policy

Every published claim should point to a snapshot plus a selector, character span, table cell or PDF page.

## 6. Extract in layers

Run structured data first, DOM attributes second, clean text third, deterministic role/location rules fourth, and model extraction last. Validate every output. Retain conflicting candidates rather than silently choosing one.

An LLM may summarise supported claims or propose candidates. It must not decide exact identity, invent a missing field, or silently override deterministic evidence.

## 7. Refresh as a diff

Use stable claim keys, immutable snapshots and idempotent upserts. A refresh produces:

- current supported value
- previous supported value
- first and last observed timestamps
- change type and materiality
- sources supporting both sides of the change

Re-crawl by source volatility: official annual accounts slowly; jobs and company news more frequently. Failed refreshes keep the last known supported value and expose the failure.

## 8. Evaluate as a control loop

Freeze development, validation and final sets with no organisation or host overlap. Hand-label exact identity, claim support and expected availability. Tune only on development, set thresholds once on validation and run the final set once.

Track:

- exact-company precision and wrong-company publications
- field precision, recall and coverage
- evidence-span validity
- static and browser-rendered crawl success
- refresh correctness and false-change rate
- cost, requests and p50/p95 time per company

Abstention is allowed and must be reported. It cannot be used to hide low coverage.

## 9. Suggested repository files

- `README.md` — setup and evaluator command
- `AGENT.md` — research and abstention policy
- `CRAWLERS.md` — connectors, budgets and fallback rules
- `IDENTITY_RESOLUTION.md` — candidate and publication gates
- `DATA_SCHEMA.md` — envelopes, claims and evidence
- `REFRESH.md` — scheduling, snapshots and diffs
- `EVAL.md` — corpus split, metrics and thresholds
- `LIMITATIONS.md` — known gaps, licences and source restrictions

## Non-negotiable limitation

LinkedIn, Meta, Glassdoor, Indeed and similar platforms have access restrictions. Use only official, licensed or otherwise permitted access. Open-source or unofficial clients may be evaluated in a private experiment, but they do not make prohibited collection permissible and cannot be the sole support for a published claim.

