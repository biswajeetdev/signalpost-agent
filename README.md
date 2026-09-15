# Signalpost agent — agent-v1

A Norwegian company research agent for the Builderr Signalpost challenge. Give it organisation
numbers; it returns exactly one terminal contract envelope per input, with a source, retrieval
time, content hash and claim span for every published fact, and an explicit availability state
(`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`) for everything else.

Accuracy comes before volume: a website or profile is published only when it carries an official
tie to this exact entity. Parent, group, administrator and similarly named sites are held back as
`ambiguous`.

## What it collects

| Module | Source | Published when |
|---|---|---|
| Legal identity, form, industry, address, employees, bankrupt/liquidation flags | Brønnøysund entity bulk snapshot | Row exists for the exact organisation number |
| Latest annual accounts (revenue, results, assets, equity, debt) | Brønnøysund `regnskapsregisteret` API, live | Returned for the organisation; reporting period on every claim |
| Filed annual-account years and official PDF copies | Brønnøysund `aarsregnskap/kopi` API, live, paced | Returned for the organisation |
| Roles (board, CEO, auditor, …) | Brønnøysund `roller/totalbestand` bulk, declared local cache | Active roles only; birth dates never stored |
| Registered workplaces | Brønnøysund `underenheter` bulk, declared local cache | Subunits whose parent is the organisation |
| Official website | Registry-declared site plus request-free domain candidates | Organisation number, registry email or registry phone on the site, or a registry-declared/unique full-legal-name domain carrying the legal name |
| Social profiles | Links on the verified company website only | Never matched by name similarity |

## Setup

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). Dependencies are pinned in `uv.lock`.

```bash
uv sync
mkdir -p cache

# Public Brønnøysund bulk downloads (NLOD 2.0). The CSV endpoints serve gzip; keep the .gz names.
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv.gz
curl -L 'https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv' -o cache/underenheter.csv.gz
curl -L 'https://data.brreg.no/enhetsregisteret/api/roller/totalbestand' -o cache/roller-totalbestand.json.gz
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

# Build the declared official cache (roles, workplaces, shared email domains/phones, name keys).
uv run python scripts/build_official_cache.py \
  --universe signalpost-universe.jsonl.gz \
  --roles cache/roller-totalbestand.json.gz \
  --subunits cache/underenheter.csv.gz \
  --entities brreg-enheter.csv.gz \
  --output cache/official.sqlite
```

## Run command

One command takes a JSONL, JSON or text batch of organisation numbers:

```bash
uv run python scripts/run_signalpost.py \
  --organisations batch.jsonl \
  --bulk brreg-enheter.csv.gz \
  --cache cache/official.sqlite \
  --output out/envelopes.jsonl \
  --profiles-output out/profiles.jsonl \
  --report out/report.json \
  --run-id daily-YYYY-MM-DD \
  --expected-count 100
```

Defaults match the locked daily budget: `--max-requests 2000`, `--max-minutes 45`, 8 workers,
14 website requests per company. Every attempt, retry, robots.txt fetch and redirect hop is charged
to the budget. Near the wall-clock limit website discovery is skipped and marked `failed`, so every
input still gets its envelope. The command exits non-zero if validation fails or the envelope count
differs from `--expected-count`.

**Refresh.** Pass the previous run's profiles to record material changes in each envelope's
`changes` list. Re-running an unchanged snapshot produces no changes:

```bash
uv run python scripts/run_signalpost.py ... --previous-profiles out/previous-profiles.jsonl
```

**Tests.** `uv run --with pytest pytest -q`

## Submitted artifact

`submission/` holds the entry run on the official 1,000-company manifest
(`select_entry_batch.py`, seed 20260823):

- `entry-companies.jsonl` — the exact organisation-number manifest
- `entry-1000-envelopes.jsonl` — 1,000 terminal envelopes
- `entry-1000-profiles.jsonl` — the profile snapshot a refresh diffs against
- `entry-1000-report.json` — runtime, requests by purpose, module states, validation

## Cost, models and APIs

- **Models:** none. No LLM or ML model runs in the evaluator command.
- **Third-party paid APIs:** none. **Expected cost per 100-company run: USD 0.**
- **Secrets:** none required; the command reads no API keys or environment secrets.
- **Measured load (100 companies):** 599 outbound requests, about 4 minutes, runtime p50 16 s and
  p95 30 s per company, well inside the 2,000-request / 45-minute budget.

## Source rights and safe handling

- **Brønnøysundregistrene** (entity, subunit and role bulk data; accounts API): open data under the
  Norwegian Licence for Open Government Data (NLOD 2.0). Person data is used only in the
  company-role context; birth dates are discarded.
- **Company websites:** only the company's own public pages (home page and at most two same-host
  contact/about pages per candidate host, at most four hosts). robots.txt is fetched and honoured
  per host, 401/403 on robots.txt means disallow. User agent:
  `builderr-signalpost-poc/0.1 (+https://builderr.ai)`.
- **URL safety:** every URL and every redirect hop passes a public-address guard (no private,
  loopback or link-local targets), redirects are capped at 5 and pages at 1.5 MB.
- **No scraping of restricted platforms.** LinkedIn, Meta, Indeed and search engines are not called
  by the evaluator command. Standalone experimental connectors under `scripts/` are not part of it.

## Known limits

- Brønnøysund's normalized accounts endpoint returns HTTP 500 for some regulated banks and
  insurers (e.g. ASA banks); those envelopes carry `financials: failed` with the source error, while
  filed years remain available.
- The frozen universe contains entities deleted after the freeze. An organisation absent from the
  bulk snapshot still gets a terminal envelope with `legal_identity: not_available`, and is listed
  in the report under `registry.absent_from_snapshot`.
- Jobs and dated public activity are not yet collected.

See `OUTPUT_CONTRACT.md` for the envelope shape and `docs/builderr/` for the challenge rules.
