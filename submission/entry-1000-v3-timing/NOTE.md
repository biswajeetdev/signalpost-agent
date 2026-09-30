1,000-company timing run on `submission/entry-companies.jsonl` from a clean clone at commit b15990a
(2026-09-30): 1000/1000 envelopes, validation passed, 2,597 s wall clock, 9,000 requests, no cache
supplied, flat company list as `--bulk`. Code at the pinned v3 commit differs only by a one-time
retry of transient website errors, stricter filtering of public_activity items and a 120-day NAV
look-back; the 100-company smoke test in `submission/smoke-100-v3/` was run at the pinned code.
