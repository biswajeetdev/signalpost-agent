# Signalpost — participant brief

Status: Open. Round 1 runs from 23 August through 21 October 2026. Random daily evaluation begins 24 August.

## What to build

Build an agent that finds company information online.

Give it a Norwegian company number. It should search company websites, public registries and other permitted sources, then return a company profile with links to the facts it found. Check that each fact belongs to the right company, include its source and date, and clearly mark information you could not find. Run it again to keep the profile current.

## What to submit

Submit your agent's code and run instructions, at least 1,000 completed company profiles, and the exact list of company numbers. The full submission checklist is below.

## How we test it

Every entered agent receives the same 100 randomly selected companies for each daily test, drawn from the full 411,160-company list. Your agent must handle company numbers it has not researched before.

## How you score

We combine independently checked findings from all submissions and Builderr's own crawlers into one reference collection. New verified findings update the collection, and every entrant is rescored against the same version. This is the checked information we have found, not a claim that we found everything online.

For each information type, 70% of its coverage score measures how many companies you covered and 30% measures how many individual facts you found. Example: the collection has 50 job postings across 20 companies. Finding 30 postings across 15 companies gives 60% of postings and 75% of companies: 70% × 75% + 30% × 60% = 70.5% for that information type. This contributes to the 35 coverage points according to its field weight; it is not the total score.

The remaining points check correctness (30), reliable updates (20), useful explanations (10) and ease of use (5). Passing also requires 65/100 overall, 21/35 coverage, 60% weighted external company recall, 95% external precision and every hard gate below. Company-matching precision is separate from factual accuracy; fabricated financial values or material wrong-company publication still fail.

The exact requirements follow.

## What the company profile should show

The product should help someone understand a company before they apply, sell, partner or invest: what it does, who leads it, where it operates, how its latest filed numbers look, whether it appears to be hiring, and what dated public activity the checked sources reveal.

Your agent must find sources for the right company, show evidence for its facts and update the profile when the information changes.

## Universe and run format

- Universe: Norway-registered entities with observed official annual-account records.
- Public universe: all 411,160 eligible organisation numbers in the frozen 2025-filer snapshot.
- Minimum entry coverage: 1,000 completed company profiles. Larger submissions—including 10,000 or the full universe—are allowed.
- Daily evaluation: the same 100 companies are randomly selected after the cutoff for every frozen agent.
- Daily schedule: 100 companies per scheduled test day through 21 October.
- Builderr supplies organisation numbers, cutoff and output contract—not the companies' official sites or social identities.
- Every frozen submission receives the same batch and resource budget.
- Full public universe: [`signalpost-company-universe-2025.jsonl.gz`](../signalpost-company-universe-2025.jsonl.gz).
- Frozen eligible universe: 411,160 active entities whose latest submitted annual-account year was 2025. Uncompressed content SHA-256: `b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838`. Download archive SHA-256: `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`.

## Required company envelope

Every input must end in one terminal envelope, even when sources are missing or blocked.

Required sections:

1. Legal identity and public brand
2. Latest annual accounts and available history
3. Leadership and registered workplaces
4. Verified official website and company-owned profiles
5. Hiring and dated public activity from permitted sources
6. Claim-level evidence and availability state
7. Refresh metadata and material changes since the previous run

Use explicit states such as `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed`. Never turn absence into zero.

## Scoring — 100 points

Scoring version 2 applies to every Round 1 entrant from 26 August 2026. All active submissions are rescored under the same rubric and current pooled-evidence version.

- 35 — Coverage and source discovery
- 30 — Accuracy, exact identity and evidence
- 20 — Refresh and extensibility
- 10 — Decision-useful synthesis
- 5 — UX and interaction

For each external field family, its coverage score is 70% company recall and 30% individual-claim recall against the independently verified union of discoveries from every submitted crawler and Builderr’s own crawlers. The union grows when any agent contributes a new verified claim; each new version is hashed and every entrant is rescored against it. The final union freezes after final-submission verification.

Qualification requires 65/100, at least 21/35 coverage, at least 60% weighted external company recall, at least 95% external precision and every hard gate. Final ranking uses mean score across completed daily batches.

## Locked run budget

- 100 input organisation numbers per daily batch
- 45 minutes wall clock
- 8 vCPU, 16 GB RAM, 10 GB temporary disk
- 2,000 outbound requests per batch, including redirects and retries
- $10 maximum declared third-party API spend per batch
- Up to four revisions; send a new exact commit hash by 18 October

## Hard gates

- At least 21/35 coverage and 60% weighted external company recall
- At least 95% external precision and no material wrong-company publication
- Submitted public artifact contains at least 1,000 completed profiles and its exact organisation-number manifest
- Exactly 100 terminal envelopes for each daily batch
- No fabricated financial values
- Claim-level source, retrieval time and reporting period where relevant
- Honest availability states
- Idempotent refresh with prior snapshots preserved
- Reproducible setup, pinned dependencies and one evaluator command
- Declared source rights, server-side secrets and safe URL handling

## Rewards

- $2,000 main final pool: $1,200 / $500 / $300
- $500 JBOX bonus pool: $250 / $150 / $100
- Four separate $100 community-vote awards on 6 September, 20 September, 4 October and 18 October

Public voting does not alter the technical ranking. Only technically qualified agents enter the hosted gallery.

## Beyond the prize

The winning builder gets the opportunity to partner with [Håvard Liltved Dalen](https://www.linkedin.com/in/liltved/) to launch Signalpost in Norway. Håvard is a Norwegian serial entrepreneur, CPO at Fronted and co-founder of JBOX.

## Start here

Try one saved example first. Requires Python 3.12+; no API key or company-data download is needed.

```bash
curl -LO https://builderr.ai/signalpost-starter-kit.tar.gz
tar -xzf signalpost-starter-kit.tar.gz
cd signalpost-starter-kit
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json` to inspect the changes and source evidence. This uses saved responses and does not qualify a competition entry.

Then follow `README.md` to install dependencies and try ten live companies. It also covers selecting at least 1,000 companies and preparing the required submission files once your practice run works.

- [Download the runnable reference agent](../signalpost-starter-kit.tar.gz)
- [Download the full 411,160-company universe](../signalpost-company-universe-2025.jsonl.gz)
- [Agent playbook](./signalpost-agent-playbook.md)
- [Learning harness](./signalpost-learning-harness.md)
- [Source policy](./signalpost-sources.md)
- [Evaluation contract](../docs/signalpost-evaluation-harness.md)
- [100-company product sample](https://builderr.ai/signalpost)

## Submit

Email `submit@builderr.ai` with the repository URL, exact commit hash, completed-profile count, organisation-number manifest, one run command, models/APIs/licences, expected cost per 100-company batch, agent name and contact for results.

## Published details awaiting clarification

The documents differ on whether the four-version limit includes the first submission. Revisions close 18 October; ask Builderr before submitting a fifth version. Handling of sparse information types and failed or missed daily runs also needs clarification. Do not infer a new scoring rule from this copy update.
