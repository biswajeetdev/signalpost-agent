# Signalpost agent — agent-v3

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
| Job postings (hiring) | NAV public job-vacancy feed (arbeidsplassen.nav.no API), read once per run | The ad is ACTIVE and its employer organisation number is the entity or one of its registered subunits; name matches only select candidates |
| Dated public activity | Verified company website: home page and one same-host news/press page, robots.txt honoured | The item has a machine-readable date (JSON-LD `datePublished` or `<time datetime>`) and a same-host URL |
| Summary | Deterministic template over the envelope's available claims | Every sentence cites the evidence ids it restates; unknowns listed; no model |

Each run also writes `<output>.html`, a self-contained searchable viewer: per-company summary, every
claim with its state, and a link to each claim's source (hover shows the supporting span).

**NAV job feed terms.** Used under NAV's API terms (https://arbeidsplassen.nav.no/vilkar-api): free for
anyone; republished ads must be removed when inactive and updated when changed, and applications must
deep-link to the source. The agent publishes only ads that are ACTIVE at run time, re-reads them every
run (an ad that becomes inactive disappears and shows as a refresh change), links `application_url`
to the source, and stores no contact persons. It uses NAV's published public token by default; an
operator can supply their own consumer token server-side via `SIGNALPOST_NAV_FEED_TOKEN`. The feed read
(~5 s per page, ~2 pages per look-back day, default 60 days) runs in the background; companies
processed before it finishes are filled after the batch, within `--max-minutes`. `--no-jobs` disables it.

## Evaluator command (clean clone, no setup beyond `uv sync`)

```bash
uv sync
uv run python scripts/run_signalpost.py \
  --organisations <evaluator batch> \
  --bulk <evaluator registry snapshot> \
  --output out/envelopes.jsonl \
  --profiles-output out/profiles.jsonl \
  --report out/report.json \
  --run-id <run id> \
  --expected-count <batch size>
```

- `--organisations`: JSON (list or wrapping object), JSONL, CSV with a header, or text; gzip or not;
  keys such as `organisation_number`, `organisasjonsnummer` or `orgnr`.
- `--bulk`: the frozen registry snapshot in any of Brønnøysund CSV, Brønnøysund JSON, or the flat
  company-list JSONL; gzip or not. Optional: without it, registry rows come from the live entity API.
- `--cache`: optional. A built cache file or a directory holding `official.sqlite` is used when
  present; otherwise roles and workplaces come from the live per-entity Brønnøysund endpoints and the
  website identity gate uses the shipped share-count snapshot `data/shared-identifiers.json.gz`.
- The reference-contract flags `--workers`, `--checkpoint-every`, `--resume` and `--modules` are
  accepted; unknown flags are ignored with a warning. Every path can also come from a
  `SIGNALPOST_*` environment variable (for example `SIGNALPOST_ORGANISATIONS`, `SIGNALPOST_CACHE`).
- No secrets or API keys are read. 100-company smoke test from a clean clone:
  `submission/smoke-100-v2/` (100/100 envelopes, validation passed, 264 s, 906 requests).

## Optional declared cache

The command does not need this. Building it trades a one-off download for fewer live requests.

```bash
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

Defaults: `--max-requests` 12 per company, `--max-minutes 45`, 8 workers, 14 website requests per
company. The rate-limited annual-account copy endpoint (one call per 2.1 s run-wide) is called last
and only while the remaining backlog fits `--max-minutes`, so the run finishes inside its budget. Every attempt, retry, robots.txt fetch and redirect hop is charged
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
- **Secrets:** none required. Optional: `SIGNALPOST_NAV_FEED_TOKEN` (an operator's own NAV consumer
  token) replaces NAV's published public token.
- **Load:** see the smoke-test report under `submission/` for the measured requests and runtime at the
  pinned commit. Brønnøysund's filing-years endpoint is paced at one request start per 2.1 s run-wide
  and is called only while its backlog fits `--max-minutes`.

## Source rights and safe handling

- **Brønnøysundregistrene** (entity, subunit and role bulk data; accounts API): open data under the
  Norwegian Licence for Open Government Data (NLOD 2.0). Person data is used only in the
  company-role context; birth dates are discarded.
- **NAV job-vacancy feed** (pam-stilling-feed.nav.no): NAV's public API under its terms of use (see
  above); only active ads, re-read every run, no contact persons stored.
- **Company websites:** only the company's own public pages (home page and a few same-host
  contact/about pages per candidate host, at most four hosts; on a verified site, one news/press page). robots.txt is fetched and honoured
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
