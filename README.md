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
input still gets its envelope. The command exits non-zero if validation fails, the envelope count
differs from `--expected-count`, or envelopes are out of input order.

**Fail-safe chunks.** The batch runs in chunks of `--chunk-size` companies (default 50), one after
another in the same process, sharing one request budget and one robots.txt cache. Each finished
chunk is checked (one valid envelope per company, in order) and saved to `--checkpoint-dir`
(default `<report>.checkpoints/` next to `--report`) before the next chunk starts. A chunk that
raises or fails its check is retried `--chunk-retries` times (default 1) on fresh copies; if it
still fails, its companies get `failed` envelopes and the run continues. Re-running the same
command with the same `--run-id` resumes: saved chunks are reused only when their fingerprint
matches (run id, organisations, registry snapshot, previous profiles, cache and settings), and
their requests and time still count against the budget. A new `--run-id` always re-crawls. The
report adds a `chunks` summary (total, passed first try, retried, fallback, resumed).

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

Run `entry-1000-2026-09-15` (code at commit `5c179c7`, registry snapshot SHA-256 `1817f874…`):
1,000 envelopes in manifest order, validation passed, USD 0. Published claims include 3,962 roles,
latest accounts for 997 companies, 872 registered workplaces, 225 verified official websites and
259 company-linked social profiles.

`submission/refresh/` holds the refresh evidence (code at commit `5ddd709`):

- `organisations.txt`, `previous-profiles.jsonl` — 100 companies and their 13 September 2026
  profiles, the previous snapshot
- `refresh-1-*` — the same companies re-crawled on 15 September with `--previous-profiles`:
  100 envelopes, validation passed, 601 requests. Every accounts, filing-history and website record
  was re-fetched; no tracked field changed in those two days, so `changes` is empty
- `refresh-2-*` — an immediate re-run against `refresh-1` profiles: 0 changes, so no false changes
- `refresh-replay.json` — `scripts/run_refresh_replay.py` on the bundled saved responses: both
  expected material changes found (employee count, annual accounts), precision 1.0, recall 1.0,
  evidence complete, idempotent re-run

## Cost, models and APIs

- **Models:** none. No LLM or ML model runs in the evaluator command.
- **Third-party paid APIs:** none. **Expected cost per 100-company run: USD 0.**
- **Secrets:** none required; the command reads no API keys or environment secrets.
- **Measured load:** 100 companies took 599 requests in about 4 minutes. The 1,000-company entry run
  took 5,745 requests (about 575 per 100 companies) in 40 minutes, runtime p50 17 s and p95 40 s per
  company. Most of that time is Brønnøysund's filing-years pacing (one request start per 2.1 s), so a
  daily 100-company batch stays well inside the 2,000-request / 45-minute budget.

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

- Brønnøysund's normalized accounts endpoint returns HTTP 500 for some regulated financial entities
  (ASA banks, insurers, securities funds, pension funds); those envelopes carry `financials: failed`
  with the source error, while filed years remain available. 2 of 1,000 in the entry run.
- Website discovery stops at 14 requests per company. When that runs out before a candidate is
  proven, the website module is `failed` with `company allowance exhausted (14)`. 39 of 1,000 in the
  entry run.
- The frozen universe contains entities deleted after the freeze. An organisation absent from the
  bulk snapshot still gets a terminal envelope with `legal_identity: not_available`, and is listed
  in the report under `registry.absent_from_snapshot`.
- A few bulk CSV rows have shifted columns (extra non-empty or missing fields; 15 in the universe).
  Their registry fields are withheld and the registry module is `failed`, rather than publishing
  misaligned values. Empty trailing extra fields are dropped harmlessly.
- Refresh reports website and company-linked social-profile changes only when both runs reached a
  conclusive website result (`available`, `not_available` or `ambiguous`). A `failed` check (for
  example the per-company allowance) is never a change: the last conclusive records are carried in
  the profile snapshot, so a real change is reported on the next conclusive run, possibly one run
  late. The committed `submission/refresh/` evidence predates website tracking (code `5ddd709`);
  re-diffing it with the current code reports 3 changes: ATEA ASA's `https://atea.com/` is no longer
  provable (its LinkedIn link is withdrawn with it) and STIFTELSEN SILDAJAZZEN's
  `https://sildajazz.no/` became verified. The immediate re-run still reports 0 changes.
- Jobs and dated public activity are not yet collected.

See `OUTPUT_CONTRACT.md` for the envelope shape and `docs/builderr/` for the challenge rules.
