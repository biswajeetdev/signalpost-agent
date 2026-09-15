import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_cached_official import build_cache  # noqa: E402
from test_pipeline import (  # noqa: E402
    official_fetcher as tp_official_fetcher,
    profile as tp_profile,
    resolver as tp_resolver,
    site_fetchers as tp_site_fetchers,
)

from norway_company_agent import chunked  # noqa: E402
from norway_company_agent.budget import RequestBudget  # noqa: E402
from norway_company_agent.cached_official import OfficialCache  # noqa: E402
from norway_company_agent.chunked import (  # noqa: E402
    CHECKPOINT_VERSION,
    check_chunk,
    chunk_fingerprint,
    divide,
    fallback_chunk,
    load_checkpoint,
    run_chunked,
    write_checkpoint,
)
from norway_company_agent.contract import validate_envelope  # noqa: E402
from norway_company_agent.pipeline import RunSettings  # noqa: E402
from norway_company_agent.pipeline import run_batch as real_run_batch  # noqa: E402

DEFAULT_SETTINGS = RunSettings(workers=2)


def org(i: int) -> str:
    return f"9{i:08d}"  # 9-digit synthetic org number, distinct from the 985589003/999999999 fixtures


def make_profiles(n: int, start: int = 1) -> list[dict]:
    return [tp_profile(org(i), f"COMPANY {i} AS") for i in range(start, start + n)]


class DivideTest(unittest.TestCase):
    def test_keeps_order_and_splits_evenly(self) -> None:
        self.assertEqual(divide([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_short_last_chunk(self) -> None:
        self.assertEqual(divide([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_size_at_least_len_gives_one_chunk(self) -> None:
        self.assertEqual(divide([1, 2, 3], 10), [[1, 2, 3]])
        self.assertEqual(divide([1, 2, 3], 3), [[1, 2, 3]])

    def test_size_below_one_raises(self) -> None:
        with self.assertRaises(ValueError):
            divide([1, 2, 3], 0)
        with self.assertRaises(ValueError):
            divide([1, 2, 3], -1)

    def test_empty_input(self) -> None:
        self.assertEqual(divide([], 2), [])


class FingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = OfficialCache(build_cache())
        self.settings = DEFAULT_SETTINGS
        self.orgs = [org(1), org(2)]

    def fp(self, **overrides):
        kwargs = dict(organisations=self.orgs, run_id="run-1", registry_sha256="sha-a", previous={}, cache=self.cache, settings=self.settings)
        kwargs.update(overrides)
        return chunk_fingerprint(**kwargs)

    def test_changes_with_run_id(self) -> None:
        self.assertNotEqual(self.fp(), self.fp(run_id="run-2"))

    def test_changes_with_organisations(self) -> None:
        self.assertNotEqual(self.fp(), self.fp(organisations=[org(1), org(3)]))

    def test_changes_with_registry_sha(self) -> None:
        self.assertNotEqual(self.fp(), self.fp(registry_sha256="sha-b"))

    def test_changes_with_previous_for_org_in_this_chunk(self) -> None:
        self.assertNotEqual(self.fp(), self.fp(previous={org(1): {"name": "changed"}}))

    def test_unaffected_by_previous_for_org_outside_this_chunk(self) -> None:
        self.assertEqual(self.fp(), self.fp(previous={org(99): {"name": "unrelated"}}))

    def test_changes_with_cache_snapshot_sha(self) -> None:
        other_cache = OfficialCache(build_cache())
        other_cache.snapshots["roles"] = dict(other_cache.snapshots["roles"], sha256="different")
        self.assertNotEqual(self.fp(), self.fp(cache=other_cache))

    def test_changes_with_each_tracked_setting(self) -> None:
        for field, value in [("discovery_allowance", 99), ("max_hosts", 1), ("unique_name_rule", False), ("min_seconds_for_discovery", 5.0)]:
            with self.subTest(field=field):
                self.assertNotEqual(self.fp(), self.fp(settings=replace(self.settings, **{field: value})))

    def test_unaffected_by_workers(self) -> None:
        self.assertEqual(self.fp(), self.fp(settings=replace(self.settings, workers=99)))

    def test_stable_for_identical_inputs(self) -> None:
        self.assertEqual(self.fp(), self.fp())


class FallbackChunkTest(unittest.TestCase):
    def test_fallback_envelopes_are_valid_ordered_and_change_free(self) -> None:
        profiles = [tp_profile(org(1), "A AS"), tp_profile(org(2), "B AS")]
        budget = RequestBudget(2000, 2700)
        envelopes, failed_profiles = fallback_chunk(profiles, "boom", budget=budget, run_id="run-1", started_at="2026-09-13T08:00:00Z", completed_at="2026-09-13T08:00:01Z")
        self.assertEqual([e["organisation_number"] for e in envelopes], [org(1), org(2)])
        for envelope in envelopes:
            self.assertEqual(validate_envelope(envelope), [])
            self.assertEqual(envelope["changes"], [])
            self.assertIn("failed", envelope["modules"].values())
        self.assertEqual(check_chunk(envelopes, failed_profiles, [org(1), org(2)]), [])


class LoadCheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "chunk-0000.json"
        self.fingerprint = "fp-a"
        self.run_id = "run-1"
        self.organisations = [org(1), org(2)]
        budget = RequestBudget(2000, 2700)
        envelopes, profiles = fallback_chunk(
            [tp_profile(org(1), "A AS"), tp_profile(org(2), "B AS")], "boom",
            budget=budget, run_id=self.run_id, started_at="t0", completed_at="t1",
        )
        self.valid_payload = {
            "version": CHECKPOINT_VERSION, "fingerprint": self.fingerprint, "index": 0,
            "organisations": self.organisations, "outcome": "passed", "attempts": 1, "problems": [],
            "started_at": "t0", "completed_at": "t1", "elapsed_seconds": 0.1,
            "envelopes": envelopes, "profiles": profiles, "operations": {"requests": 0, "by_purpose": {}},
        }

    def load(self, fingerprint=None, run_id=None):
        return load_checkpoint(self.path, fingerprint if fingerprint is not None else self.fingerprint, self.organisations, run_id if run_id is not None else self.run_id)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self.load())

    def test_corrupt_json_returns_none_without_raising(self) -> None:
        self.path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(self.load())

    def test_valid_checkpoint_loads(self) -> None:
        write_checkpoint(self.path, self.valid_payload)
        loaded = self.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["envelopes"], self.valid_payload["envelopes"])

    def test_version_mismatch_returns_none(self) -> None:
        write_checkpoint(self.path, dict(self.valid_payload, version=CHECKPOINT_VERSION + 1))
        self.assertIsNone(self.load())

    def test_fingerprint_mismatch_returns_none(self) -> None:
        write_checkpoint(self.path, self.valid_payload)
        self.assertIsNone(self.load(fingerprint="different-fp"))

    def test_run_id_mismatch_returns_none(self) -> None:
        # Never reused even if the fingerprint (which also encodes run_id) somehow matched — belt-and-braces.
        write_checkpoint(self.path, self.valid_payload)
        self.assertIsNone(self.load(run_id="run-2"))

    def test_fallback_outcome_is_never_reused(self) -> None:
        write_checkpoint(self.path, dict(self.valid_payload, outcome="fallback"))
        self.assertIsNone(self.load())

    def test_envelopes_failing_check_chunk_return_none(self) -> None:
        broken = deepcopy(self.valid_payload)
        broken["envelopes"][0]["claims"].append({"field": "x", "value": "y", "availability": "available", "evidence_ids": ["ev-missing"]})
        write_checkpoint(self.path, broken)
        self.assertIsNone(self.load())

    def test_malformed_shape_returns_none_instead_of_raising(self) -> None:
        for mutation in (
            lambda payload: {**payload, "envelopes": [{**payload["envelopes"][0], "run": "not-a-dict"}, payload["envelopes"][1]]},
            lambda payload: {**payload, "elapsed_seconds": True},
            lambda payload: {**payload, "operations": {**payload["operations"], "requests": "3"}},
            lambda payload: {**payload, "started_at": None},
        ):
            with self.subTest(mutation=mutation):
                write_checkpoint(self.path, mutation(deepcopy(self.valid_payload)))
                self.assertIsNone(self.load())

    def test_atomic_write_leaves_no_tmp_file_behind(self) -> None:
        write_checkpoint(self.path, self.valid_payload)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(self.path.suffix + ".tmp").exists())


class RunChunkedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = OfficialCache(build_cache())

    def checkpoint_dir(self, name: str = "checkpoints") -> Path:
        return self.tmp / name

    def run_chunked(self, profiles, **overrides):
        kwargs = dict(
            cache=self.cache, budget=RequestBudget(2000, 2700), run_id="run-1",
            settings=DEFAULT_SETTINGS, previous={}, registry_sha256="sha-registry-a",
            checkpoint_dir=self.checkpoint_dir(), chunk_size=2, chunk_retries=1,
            official_fetcher=tp_official_fetcher, site_fetchers=tp_site_fetchers, resolver=tp_resolver,
        )
        kwargs.update(overrides)
        return run_chunked(profiles, **kwargs)

    def spying_run_batch(self, fail=None):
        # (side_effect, calls): calls[org_tuple] counts invocations against the real run_batch;
        # fail(org_tuple, attempt_number_for_that_chunk) may return an Exception to raise instead.
        calls: Counter = Counter()

        def side_effect(profiles_, **kwargs):
            key = tuple(p["organisation_number"] for p in profiles_)
            calls[key] += 1
            error = fail(key, calls[key]) if fail else None
            if error:
                raise error
            return real_run_batch(profiles_, **kwargs)

        return side_effect, calls

    def test_happy_path_merges_in_order_validates_and_stays_within_budget(self) -> None:
        profiles = make_profiles(5)
        budget = RequestBudget(2000, 2700)
        envelopes, merged_profiles, report = self.run_chunked(profiles, chunk_size=2, budget=budget)
        expected = [org(i) for i in range(1, 6)]
        self.assertEqual([e["organisation_number"] for e in envelopes], expected)
        self.assertEqual([p["organisation_number"] for p in merged_profiles], expected)
        self.assertEqual(len(set(expected)), len(expected))
        for envelope in envelopes:
            self.assertEqual(validate_envelope(envelope), [], envelope["organisation_number"])
        self.assertTrue(report["validation"]["passed"], report["validation"])
        self.assertLessEqual(budget.used, budget.max_requests)
        self.assertEqual(budget.used, report["operations"]["requests"])
        self.assertEqual(budget.used, sum(e["operations"]["requests"] for e in envelopes))
        chunks_report = report["chunks"]
        self.assertEqual(chunks_report["total"], 3)
        self.assertEqual(chunks_report["passed_first_try"], 3)
        self.assertEqual(chunks_report["retried"], 0)
        self.assertEqual(chunks_report["fallback"], 0)
        self.assertEqual(chunks_report["resumed"], 0)
        self.assertEqual(chunks_report["fallback_organisations"], [])
        for module, counts in report["module_states"].items():
            self.assertEqual(sum(counts.values()), len(envelopes), module)
        self.assertEqual(len(list(self.checkpoint_dir().glob("chunk-*.json"))), 3)

    def test_robots_cache_is_shared_across_chunks(self) -> None:
        seen_ids = []

        def site_fetchers(budget, company, *, allowance, robots):
            seen_ids.append(id(robots))
            return tp_site_fetchers(budget, company, allowance=allowance, robots=robots)

        self.run_chunked(make_profiles(6), chunk_size=2, site_fetchers=site_fetchers)
        self.assertGreaterEqual(len(seen_ids), 3)
        self.assertEqual(len(set(seen_ids)), 1)

    def test_chunk_failure_is_isolated_transient_retries_persistent_falls_back(self) -> None:
        profiles = make_profiles(5)  # chunks: [1,2] [3,4] [5]
        target = {org(3), org(4)}
        cases = {
            # (fail predicate, expected chunk-0001 outcome, passed_first_try, retried, fallback)
            "transient_then_succeeds": (lambda key, n: RuntimeError("transient boom") if set(key) == target and n == 1 else None, "retried", 2, 1, 0),
            "persistent_always_fails": (lambda key, n: RuntimeError("permanent boom") if set(key) == target else None, "fallback", 2, 0, 1),
        }
        for label, (fail, outcome, passed_first_try, retried, fallback) in cases.items():
            with self.subTest(label=label):
                side_effect, _ = self.spying_run_batch(fail)
                checkpoint_dir = self.checkpoint_dir(f"isolation-{label}")
                with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
                    envelopes, _, report = self.run_chunked(profiles, chunk_size=2, chunk_retries=1, checkpoint_dir=checkpoint_dir)
                self.assertEqual(len(envelopes), 5)
                for envelope in envelopes:
                    self.assertEqual(validate_envelope(envelope), [])
                chunks_report = report["chunks"]
                self.assertEqual(chunks_report["passed_first_try"], passed_first_try)
                self.assertEqual(chunks_report["retried"], retried)
                self.assertEqual(chunks_report["fallback"], fallback)
                payload = json.loads((checkpoint_dir / "chunk-0001.json").read_text())
                self.assertEqual(payload["outcome"], outcome)
                self.assertEqual(payload["attempts"], 2)
                if outcome == "fallback":
                    self.assertEqual(sorted(chunks_report["fallback_organisations"]), sorted(target))
                    failed = {e["organisation_number"]: e for e in envelopes if e["organisation_number"] in target}
                    for envelope in failed.values():
                        self.assertEqual(envelope["changes"], [])
                        self.assertIn("failed", envelope["modules"].values())
                else:
                    self.assertEqual(payload["problems"], [])  # no attempt-1 failure leftovers

    def test_resume_after_crash_skips_completed_chunks(self) -> None:
        profiles = make_profiles(5)  # chunks: [1,2] [3,4] [5]
        checkpoint_dir = self.checkpoint_dir("resume")
        call_count = 0

        def crash_on_second_chunk(profiles_, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise SystemExit("simulated crash")  # not an Exception subclass: aborts run_chunked entirely
            return real_run_batch(profiles_, **kwargs)

        with mock.patch.object(chunked, "run_batch", side_effect=crash_on_second_chunk):
            with self.assertRaises(SystemExit):
                self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
        self.assertEqual(sorted(p.name for p in checkpoint_dir.glob("chunk-*.json")), ["chunk-0000.json"])

        side_effect, calls = self.spying_run_batch()
        with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
            envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)

        self.assertEqual(sum(calls.values()), 2)  # only the two never-checkpointed chunks re-ran
        self.assertEqual(report["chunks"]["resumed"], 1)
        self.assertEqual(len(envelopes), 5)
        self.assertEqual([e["organisation_number"] for e in envelopes], [org(i) for i in range(1, 6)])
        for envelope in envelopes:
            self.assertEqual(validate_envelope(envelope), [])

        checkpoint0 = json.loads((checkpoint_dir / "chunk-0000.json").read_text())
        resumed = next(e for e in envelopes if e["organisation_number"] == org(1))
        checkpointed = next(e for e in checkpoint0["envelopes"] if e["organisation_number"] == org(1))
        self.assertEqual(resumed, checkpointed)  # loaded verbatim; run_id already matches (reuse requires it)

    def test_resume_only_reruns_the_invalidated_chunk(self) -> None:
        profiles = make_profiles(4)  # chunks: [1,2] [3,4]
        # (invalidate(checkpoint_dir), previous mapping, expected re-run org tuple)
        cases = {
            "deleted_checkpoint": (lambda d: (d / "chunk-0001.json").unlink(), {}, (org(3), org(4))),
            "stale_previous": (lambda d: None, {org(1): {"organisation_number": org(1), "name": "CHANGED NAME AS"}}, (org(1), org(2))),
        }
        for label, (invalidate, previous, expected_rerun) in cases.items():
            with self.subTest(label=label):
                checkpoint_dir = self.checkpoint_dir(f"invalidate-{label}")
                self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
                invalidate(checkpoint_dir)

                side_effect, calls = self.spying_run_batch()
                with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
                    envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, previous=previous)

                self.assertEqual(sum(calls.values()), 1)
                self.assertEqual(list(calls)[0], expected_rerun)
                self.assertEqual(report["chunks"]["resumed"], 1)
                self.assertEqual(len(envelopes), 4)

    def test_corrupt_checkpoint_file_is_rerun_without_crashing(self) -> None:
        profiles = make_profiles(2)
        checkpoint_dir = self.checkpoint_dir("corrupt")
        self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
        (checkpoint_dir / "chunk-0000.json").write_text("{not valid json", encoding="utf-8")
        envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(report["chunks"]["resumed"], 0)

    def test_refresh_reports_no_changes_across_chunks(self) -> None:
        _, first_profiles, _ = self.run_chunked(make_profiles(4), chunk_size=2, checkpoint_dir=self.checkpoint_dir("refresh-first"))
        previous = {p["organisation_number"]: p for p in first_profiles}
        envelopes, _, _ = self.run_chunked(make_profiles(4), chunk_size=2, checkpoint_dir=self.checkpoint_dir("refresh-second"), previous=previous, run_id="run-2")
        for envelope in envelopes:
            self.assertEqual(envelope["changes"], [])

    def test_different_run_id_never_reuses_checkpoint(self) -> None:
        # Resume must only continue the SAME run: a new run_id has to re-crawl live sources, or a
        # refresh evaluation would silently get stale data for a run that claims to be fresh.
        profiles = make_profiles(4)
        checkpoint_dir = self.checkpoint_dir("run-id-fingerprint")
        side_effect, calls = self.spying_run_batch()

        with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
            self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-1")
        self.assertEqual(sum(calls.values()), 2)

        calls.clear()
        with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
            envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-2")

        self.assertEqual(sum(calls.values()), 2, "expected both chunks to re-run under a new run_id")
        self.assertEqual(report["chunks"]["resumed"], 0)
        self.assertTrue(all(e["run"]["run_id"] == "run-2" for e in envelopes))

    def test_same_run_id_resume_reuses_checkpoints(self) -> None:
        profiles = make_profiles(4)
        checkpoint_dir = self.checkpoint_dir("same-run-id")
        self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-1")

        side_effect, calls = self.spying_run_batch()
        with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
            _, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-1")

        self.assertEqual(sum(calls.values()), 0)
        self.assertEqual(report["chunks"]["resumed"], 2)

    def test_tampered_envelope_run_id_on_disk_forces_rerun_but_sibling_still_resumes(self) -> None:
        # The fingerprint alone can't catch this: it's computed from the *call's* run_id, not from what's
        # stored under envelope["run"]["run_id"]. _checkpoint_matches_run has to reject a checkpoint
        # whose fingerprint matches but whose stored run_id doesn't.
        profiles = make_profiles(4)  # chunks: [1,2] [3,4]
        checkpoint_dir = self.checkpoint_dir("tampered-run-id")
        self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-1")

        chunk0_path = checkpoint_dir / "chunk-0000.json"
        payload = json.loads(chunk0_path.read_text())
        payload["envelopes"][0]["run"]["run_id"] = "some-other-run"
        chunk0_path.write_text(json.dumps(payload), encoding="utf-8")

        side_effect, calls = self.spying_run_batch()
        with mock.patch.object(chunked, "run_batch", side_effect=side_effect):
            envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, run_id="run-1")

        self.assertEqual(sum(calls.values()), 1)  # only the tampered chunk re-ran, no exception
        self.assertEqual(list(calls)[0], (org(1), org(2)))
        self.assertEqual(report["chunks"]["resumed"], 1)  # chunk 2 (untouched) was still resumed
        self.assertEqual(len(envelopes), 4)
        for envelope in envelopes:
            self.assertEqual(validate_envelope(envelope), [])
            self.assertEqual(envelope["run"]["run_id"], "run-1")

    def test_malformed_checkpoint_with_matching_fingerprint_reruns_without_crashing(self) -> None:
        profiles = make_profiles(2)
        checkpoint_dir = self.checkpoint_dir("malformed")
        mutations = {
            "missing_envelope_operations": lambda payload: payload["envelopes"][0].pop("operations"),
            "envelope_requests_as_string": lambda payload: payload["envelopes"][0]["operations"].__setitem__("requests", "3"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                shutil.rmtree(checkpoint_dir, ignore_errors=True)
                self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
                path = checkpoint_dir / "chunk-0000.json"
                payload = json.loads(path.read_text())
                mutate(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
                self.assertEqual(len(envelopes), 2)
                self.assertEqual(report["chunks"]["resumed"], 0)

    def test_resume_charges_prior_budget_and_wall_clock_into_the_new_run(self) -> None:
        profiles = make_profiles(4)  # chunks: [1,2] [3,4]
        checkpoint_dir = self.checkpoint_dir("resume-charges")
        self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir)
        path = checkpoint_dir / "chunk-0000.json"
        payload = json.loads(path.read_text())
        chunk0_requests = payload["operations"]["requests"]
        payload["elapsed_seconds"] = 2650.0  # not part of the fingerprint, so the checkpoint still loads
        path.write_text(json.dumps(payload), encoding="utf-8")
        (checkpoint_dir / "chunk-0001.json").unlink()  # force chunk 2 to re-run fresh, after the deadline shrinks

        budget = RequestBudget(2000, 2700)
        settings = replace(DEFAULT_SETTINGS, min_seconds_for_discovery=120.0)
        envelopes, _, report = self.run_chunked(profiles, chunk_size=2, checkpoint_dir=checkpoint_dir, budget=budget, settings=settings)

        self.assertEqual(report["chunks"]["resumed"], 1)
        self.assertEqual(report["operations"]["resumed_requests"], chunk0_requests)
        fresh = [e for e in envelopes if e["organisation_number"] in {org(3), org(4)}]
        self.assertEqual(budget.used, chunk0_requests + sum(e["operations"]["requests"] for e in fresh))
        self.assertEqual(report["operations"]["requests"], budget.used)
        self.assertLess(budget.seconds_left(), settings.min_seconds_for_discovery)  # ~2700-2650s charged prior
        for envelope in fresh:
            self.assertEqual(envelope["modules"]["website"], "failed")
            self.assertTrue(any("wall-clock" in err["message"] for err in envelope["errors"]), envelope["errors"])

    def test_refresh_changes_match_unchunked_run_per_org(self) -> None:
        # The zero-change idempotency test above can't catch a diffing mismatch ([] == [] either way);
        # this forces a materially different `previous` for one org and compares `changes` org by org.
        previous = {org(1): {"organisation_number": org(1), "name": "OLD NAME AS"}}

        chunked_envelopes, _, _ = self.run_chunked(make_profiles(4), chunk_size=2, checkpoint_dir=self.checkpoint_dir("refresh-parity"), previous=previous)
        unchunked_envelopes, _, _ = real_run_batch(
            make_profiles(4), cache=self.cache, budget=RequestBudget(2000, 2700), run_id="run-1",
            settings=DEFAULT_SETTINGS, previous=previous,
            official_fetcher=tp_official_fetcher, site_fetchers=tp_site_fetchers, resolver=tp_resolver,
        )

        chunked_changes = {e["organisation_number"]: e["changes"] for e in chunked_envelopes}
        unchunked_changes = {e["organisation_number"]: e["changes"] for e in unchunked_envelopes}
        self.assertTrue(chunked_changes[org(1)], "expected a detected change for org(1)")
        for key in chunked_changes:
            self.assertEqual(chunked_changes[key], unchunked_changes[key], key)


if __name__ == "__main__":
    unittest.main()
