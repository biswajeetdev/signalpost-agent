# Signalpost evaluation contract

Status: scoring version 2, effective 26 August 2026 for every Round 1 entrant. Random daily evaluation began 24 August; all active submissions are rescored under version 2.

## In plain language

Build a program that researches companies and keeps their profiles current. Submit at least 1,000 completed profiles; the daily test uses the same 100 random companies for every entered agent, drawn from the full 411,160-company list.

We combine independently checked findings from all submissions and Builderr's own crawlers into one reference collection. New verified findings update the collection, and every entrant is rescored against the same version. This is the checked information we have found, not a claim that we found everything online.

For each information type, 70% of its coverage score measures how many companies you covered and 30% measures how many individual facts you found. Example: the collection has 50 job postings across 20 companies. Finding 30 postings across 15 companies gives 60% of postings and 75% of companies: 70% × 75% + 30% × 60% = 70.5% for that information type. This contributes to the 35 coverage points according to its field weight; it is not the total score.

The remaining points check correctness (30), reliable updates (20), useful explanations (10) and ease of use (5). Passing also requires 65/100 overall, 21/35 coverage, 60% weighted external company recall, 95% external precision and every hard gate below. Company-matching precision is separate from factual accuracy; fabricated financial values or material wrong-company publication still fail.

The exact requirements follow.

## Corpus

- Universe: Norway-registered entities with observed annual-account records.
- Public universe: all 411,160 eligible organisation numbers.
- Minimum submitted artifact: 1,000 completed profiles plus its exact organisation-number manifest. Larger submissions are allowed.
- Public product sample: 100 profiles at `/signalpost`.
- Daily evaluation: the same 100 organisations are randomly selected after the cutoff for every frozen agent, on each scheduled test day through 21 October.
- The public universe is not hidden. Freshness, exact attribution and reproducible execution—not secrecy of the organisation number—are what the daily run tests.
- Every entrant receives the same batch, cutoff, network policy and resource budget.
- Eligible-universe uncompressed content SHA-256: `b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838`.
- Public `.jsonl.gz` archive SHA-256: `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`.

## Output contract

Exactly one terminal envelope per input organisation number. Envelopes must contain legal identity, claims, evidence references, availability states, source snapshots, refresh metadata and errors. Valid states are `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed`.

## Scoring — 100

1. Coverage and source discovery — 35
2. Accuracy, exact identity and evidence — 30
3. Refresh and extensibility — 20
4. Decision-useful synthesis — 10
5. UX and interaction — 5

Coverage and exact attribution control 65 points. For every external field family, coverage is 70% company recall and 30% individual-claim recall against the independently verified union of discoveries from every submitted crawler and Builderr’s own crawlers. The union is cumulative and versioned: each verified addition creates a new pool hash and every entrant is rescored against that same latest version. The final union freezes only after all eligible final submissions have been verified.

## Hard gates

- At least 21/35 coverage.
- At least 60% weighted external company recall across measurable field families.
- At least 95% external exact-entity precision.
- The submitted public artifact has at least 1,000 completed profiles and a matching manifest.
- Every daily 100-company input produces exactly 100 terminal envelopes.
- No fabricated financial value or material wrong-company publication.
- Published material claims have source, retrieval time and reporting period where relevant.
- Missing values are never silently converted to zero.
- Re-running the same snapshot is idempotent.
- Refresh preserves prior evidence and exposes material changes.
- Setup is reproducible with pinned dependencies and one evaluator command.
- Source rights, secrets and outbound URL policy are documented and safe.

Qualification is 65/100 plus all hard gates. Final ranking uses the mean score across completed daily batches. Ties break on fewer wrong-company publications, then higher weighted company recall, then lower declared third-party cost.

## Locked evaluator budget

- 100 inputs
- 45 minutes wall clock
- 8 vCPU, 16 GB RAM and 10 GB temporary disk
- 2,000 total outbound requests, including redirects and retries
- $10 maximum declared third-party API spend
- server-side secrets supplied through documented environment variables only

Builderr provides the frozen official registry snapshot used for identity anchoring. External caches must be declared. Cached public-universe material is allowed, but the daily score still enforces source timestamps, refresh behavior and the same evidence cutoff for everyone.

Entrants may submit up to four exact commit hashes. Revisions close 18 October 2026 and take effect only after being frozen for the next daily run.

## Measurement

Report exact-company precision, wrong-company publications, per-field precision/recall/coverage, evidence-span validity, crawl completion, refresh correctness, false-change rate, cost per company, request count, and p50/p95 runtime. Abstention is reported separately and cannot satisfy coverage.

## Public voting

Only qualified entries enter the hosted Builderr gallery. A $100 public-vote award runs every fortnight. Votes never alter the final technical score or ranking.

## Local checks

```bash
npm run check:signalpost
npm run check:signalpost-showcase
npm run check:signalpost-challenge
```

## Published details awaiting clarification

The documents differ on whether the four-version limit includes the first submission. Revisions close 18 October; ask Builderr before submitting a fifth version. Handling of sparse information types and failed or missed daily runs also needs clarification. Do not infer a new scoring rule from this copy update.
