"""Comparing two runs.

The fixtures here are deliberately the same ones as
``viewer/src/__tests__/diff.test.ts``.  The command line diff and the viewer's
Diff screen are two implementations of one algorithm, and the failure mode
that matters is not either of them being wrong on its own -- it is the two
quietly disagreeing about which call path grew.  Sharing the fixtures is what
makes that visible.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from performer import diff as diff_mod
from performer import fake
from performer.cli import main


def run_cli(*argv):
    """Run the CLI in-process and capture (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


def folded(text: str) -> List[Tuple[str, int]]:
    entries = []
    for line in text.strip().splitlines():
        stack, _, value = line.strip().rpartition(" ")
        entries.append((stack, int(value)))
    return entries


A = folded(
    """
    worker;run();lock();__lll_lock_wait 100
    worker;run();compute() 300
    worker;run();park() 100
    """
)
B = folded(
    """
    worker;run();lock();__lll_lock_wait 500
    worker;run();compute() 300
    worker;run();backpressure();wait() 200
    """
)


class NormalisePathTests(unittest.TestCase):
    def test_offsets_are_stripped_so_a_rebuild_is_not_a_rewrite(self):
        self.assertEqual(
            diff_mod.normalise_path("app+1;run()+0x20;work()+4", merge_threads=False),
            "app;run();work()",
        )

    def test_operator_plus_survives(self):
        self.assertEqual(
            diff_mod.normalise_path("Vec::operator++", merge_threads=False),
            "Vec::operator++",
        )

    def test_unresolved_address_is_named_not_hidden(self):
        self.assertEqual(
            diff_mod.normalise_path("app;0x7f3c8a001240", merge_threads=False),
            "app;[unknown]",
        )

    def test_merge_drops_the_thread_frame(self):
        self.assertEqual(
            diff_mod.normalise_path("worker7;run();f()", merge_threads=True),
            "run();f()",
        )

    def test_merge_keeps_a_single_frame_stack(self):
        # Dropping the only frame would turn a real sample into an empty path.
        self.assertEqual(
            diff_mod.normalise_path("[unknown]", merge_threads=True), "[unknown]"
        )


class DiffStacksTests(unittest.TestCase):
    def setUp(self):
        self.diff = diff_mod.diff_stacks(A, B, merge_threads=False, min_share=0)
        self.by_path = {entry.path: entry for entry in self.diff.paths}

    def test_totals_are_reported_raw(self):
        self.assertEqual(self.diff.raw_total_a, 500)
        self.assertEqual(self.diff.raw_total_b, 1000)

    def test_shares_not_counts(self):
        lock = self.by_path["worker;run();lock();__lll_lock_wait"]
        self.assertAlmostEqual(lock.a, 0.2)
        self.assertAlmostEqual(lock.b, 0.5)
        self.assertAlmostEqual(lock.delta, 0.3)

    def test_an_unchanged_count_is_a_fallen_share(self):
        # 300 samples in both runs, but a smaller slice of the longer one.
        # Reporting this as "unchanged" answers a question nobody asked.
        compute = self.by_path["worker;run();compute()"]
        self.assertAlmostEqual(compute.a, 0.6)
        self.assertAlmostEqual(compute.b, 0.3)
        self.assertAlmostEqual(compute.delta, -0.3)

    def test_raw_mode_compares_counts(self):
        raw = diff_mod.diff_stacks(A, B, merge_threads=False, min_share=0, normalise=False)
        by_path = {entry.path: entry for entry in raw.paths}
        self.assertEqual(by_path["worker;run();compute()"].delta, 0)
        self.assertEqual(
            by_path["worker;run();lock();__lll_lock_wait"].delta, 400
        )

    def test_one_sided_paths_are_kept_apart(self):
        self.assertEqual(
            [entry.path for entry in self.diff.only_in_b],
            ["worker;run();backpressure();wait()"],
        )
        self.assertEqual(
            [entry.path for entry in self.diff.only_in_a], ["worker;run();park()"]
        )

    def test_a_new_path_has_no_ratio(self):
        # "+infinity%" and "+0%" would both be lies about a path that did not
        # exist before.
        self.assertIsNone(self.diff.only_in_b[0].ratio)

    def test_sorted_by_size_of_change(self):
        magnitudes = [abs(entry.delta) for entry in self.diff.paths]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))
        self.assertEqual(
            sorted(entry.path for entry in self.diff.paths[:2]),
            ["worker;run();compute()", "worker;run();lock();__lll_lock_wait"],
        )

    def test_growth_and_shrinkage_balance_once_normalised(self):
        self.assertAlmostEqual(self.diff.grew, self.diff.shrank)

    def test_merging_combines_the_same_path_in_two_threads(self):
        per_thread = folded("t1;run();f() 10\nt2;run();f() 10")
        merged = diff_mod.diff_stacks(
            per_thread, per_thread, merge_threads=True, min_share=0
        )
        self.assertEqual(len(merged.paths), 1)
        self.assertEqual(merged.paths[0].path, "run();f()")

    def test_thread_filter_matches_the_original_root(self):
        per_thread = folded("keepme;run();f() 10\nother;run();f() 90")
        filtered = diff_mod.diff_stacks(
            per_thread, per_thread, merge_threads=True, thread_filter="keep", min_share=0
        )
        self.assertEqual(filtered.raw_total_a, 10)

    def test_noise_floor_is_counted_not_hidden(self):
        noisy = folded("t;big() 10000\nt;tiny() 1")
        result = diff_mod.diff_stacks(noisy, noisy, merge_threads=False, min_share=0.001)
        self.assertEqual(len(result.paths), 1)
        self.assertEqual(result.below_threshold, 1)

    def test_empty_run_does_not_divide_by_zero(self):
        result = diff_mod.diff_stacks([], B, merge_threads=False, min_share=0)
        self.assertEqual(result.raw_total_a, 0)
        self.assertEqual(len(result.only_in_b), 3)
        self.assertTrue(all(entry.delta == entry.b for entry in result.paths))


class ThreadDiffTests(unittest.TestCase):
    """Threads are compared by name because tids are not stable across runs."""

    def doc(self, entries):
        return {
            "schema_version": 1,
            "threads": {
                str(tid): {
                    "name": name,
                    "start_schedstat": {"run_ns": 0, "wait_ns": 0, "timeslices": 0},
                    "end_schedstat": {
                        "run_ns": run_ms * 1_000_000,
                        "wait_ns": wait_ms * 1_000_000,
                        "timeslices": 0,
                    },
                }
                for tid, name, run_ms, wait_ms in entries
            },
        }

    def test_same_pool_different_tids_is_not_a_change(self):
        a = self.doc([(101, "worker", 1000, 100), (102, "worker", 1000, 100)])
        b = self.doc([(901, "worker", 1000, 100), (902, "worker", 1000, 100)])
        rows = diff_mod.diff_threads(a, 10.0, b, 10.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].count_a, 2)
        self.assertEqual(rows[0].count_b, 2)
        self.assertEqual(rows[0].cpu_delta, 0)

    def test_a_grown_pool_shows_as_a_count_change(self):
        a = self.doc([(1, "worker", 1000, 100)])
        b = self.doc([(1, "worker", 1000, 100), (2, "worker", 1000, 400)])
        rows = diff_mod.diff_threads(a, 10.0, b, 10.0)
        self.assertEqual(rows[0].count_delta, 1)
        self.assertAlmostEqual(rows[0].wait_a, 10.0)
        self.assertAlmostEqual(rows[0].wait_b, 50.0)

    def test_times_are_per_second_so_run_lengths_do_not_matter(self):
        a = self.doc([(1, "worker", 6000, 600)])
        b = self.doc([(1, "worker", 9000, 900)])
        rows = diff_mod.diff_threads(a, 60.0, b, 90.0)
        self.assertAlmostEqual(rows[0].cpu_a, 100.0)
        self.assertAlmostEqual(rows[0].cpu_b, 100.0)
        self.assertAlmostEqual(rows[0].cpu_delta, 0.0)

    def test_missing_inventory_is_not_a_crash(self):
        self.assertEqual(diff_mod.diff_threads(None, 60.0, None, 60.0), [])


class LoadScenarioTests(TempDirTestCase):
    """The synthetic pair the diff is developed and demonstrated against."""

    def test_same_seed_different_load_keeps_the_structure(self):
        # Two bundles that differ only by random seed are a poor fixture: the
        # differences are noise, evenly spread, and a diff that surfaces them
        # is not doing anything. The same seed at two loads has to be the same
        # application working harder.
        steady, _ = fake.generate(
            self.tmp, label="steady", seed=7, load="steady", pack=False
        )
        heavy, _ = fake.generate(
            self.tmp, label="heavy", seed=7, load="heavy", pack=False
        )
        a = _read_folded(steady / "stacks" / "oncpu.folded")
        b = _read_folded(heavy / "stacks" / "oncpu.folded")

        result = diff_mod.diff_stacks(a, b, merge_threads=True, min_share=0)

        # The timer mutex is the thing the whole project suspects, and the
        # heavy scenario is what a diff has to be able to find.
        movers = [entry for entry in result.paths[:12] if entry.delta > 0]
        self.assertTrue(
            any("__lll_lock_wait" in entry.path for entry in movers),
            f"lock contention not among the top movers: {[e.path for e in movers]}",
        )
        # Backpressure code that only runs at the higher load.
        self.assertTrue(
            any("Backpressure::throttle" in entry.path or "EventQueue::grow" in entry.path
                for entry in result.only_in_b),
            f"new call paths not found: {[e.path for e in result.only_in_b[:5]]}",
        )
        # And nothing should have disappeared: the heavier run does everything
        # the lighter one did.
        self.assertEqual([entry.path for entry in result.only_in_a], [])

    def test_unknown_scenario_is_a_clean_error(self):
        with self.assertRaises(Exception) as caught:
            fake.generate(self.tmp, label="x", load="nope", pack=False)
        self.assertIn("unknown load scenario", str(caught.exception))


class DiffCommandTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        _, self.a = fake.generate(
            self.tmp, label="steady", duration_s=60, seed=99, load="steady"
        )
        _, self.b = fake.generate(
            self.tmp, label="heavy", duration_s=90, seed=99, load="heavy"
        )

    def test_text_output_names_the_runs_and_the_movers(self):
        code, out, _ = run_cli("diff", str(self.a), str(self.b))
        self.assertEqual(code, 0)
        self.assertIn("steady", out)
        self.assertIn("heavy", out)
        self.assertIn("oncpu", out)
        self.assertIn("only in B (new call paths)", out)
        self.assertIn("threads by name", out)
        # 60 s against 90 s: comparing raw counts here would be wrong, and the
        # output has to say which it did.
        self.assertIn("share of each run", out)

    def test_raw_mode_says_so(self):
        code, out, _ = run_cli("diff", str(self.a), str(self.b), "--raw")
        self.assertEqual(code, 0)
        self.assertIn("compared as raw", out)

    def test_json_output_is_machine_readable(self):
        code, out, _ = run_cli("diff", str(self.a), str(self.b), "--json", "--top", "5")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["a"]["label"], "steady")
        self.assertEqual(doc["b"]["label"], "heavy")
        kinds = {entry["kind"] for entry in doc["stacks"]}
        self.assertIn("oncpu", kinds)
        oncpu = next(e for e in doc["stacks"] if e["kind"] == "oncpu")
        self.assertLessEqual(len(oncpu["movers"]), 5)
        self.assertTrue(oncpu["normalised"])
        for mover in oncpu["movers"]:
            self.assertAlmostEqual(mover["delta"], mover["b"] - mover["a"], places=9)

    def test_selecting_one_kind(self):
        code, out, _ = run_cli("diff", str(self.a), str(self.b), "--kind", "futex")
        self.assertEqual(code, 0)
        self.assertIn("futex", out)
        self.assertNotIn("\noncpu ", out)

    def test_missing_bundle_is_a_clean_error(self):
        code, _, err = run_cli("diff", str(self.a), str(self.tmp / "nope.tgz"))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_caveat_when_one_run_is_unusable(self):
        _, broken = fake.generate(
            self.tmp, label="noframes", seed=99, bad_frame_pointers=True
        )
        code, out, _ = run_cli("diff", str(self.a), str(broken))
        self.assertEqual(code, 0)
        self.assertIn("unresolved frames", out)

    def test_agrees_with_the_viewer_on_the_shared_fixture(self):
        """The number the TypeScript test asserts, from the Python side.

        `viewer/src/__tests__/diff.test.ts` asserts a 0.2 -> 0.5 share and a
        +0.3 delta for the lock path on this exact input. If the two ever
        disagree, one of the two tools is lying to somebody.
        """
        result = diff_mod.diff_stacks(A, B, merge_threads=False, min_share=0)
        lock = next(e for e in result.paths if e.path.endswith("__lll_lock_wait"))
        self.assertAlmostEqual(lock.a, 0.2)
        self.assertAlmostEqual(lock.b, 0.5)
        self.assertAlmostEqual(lock.delta, 0.3)


def _read_folded(path: Path) -> List[Tuple[str, int]]:
    return folded(path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
