"""/proc readers, exercised against a real multi threaded process."""

from __future__ import annotations

import os
import unittest

from performer import layout, proc

from unittest import mock

from .support import launcher_chain, python_sleeper, reap, requires_target, spawn_target, wait_until


class SelfTests(unittest.TestCase):
    """The current process is a target like any other."""

    def test_stat_of_self(self):
        stat = proc.read_stat(os.getpid())
        self.assertIsNotNone(stat)
        self.assertEqual(stat.pid, os.getpid())
        self.assertGreaterEqual(stat.num_threads, 1)
        self.assertGreater(stat.start_time_ticks, 0)

    def test_missing_pid_returns_none_not_an_exception(self):
        # /proc readers are called in a loop over threads that come and go, so
        # a vanished pid must be an ordinary None, never a raised exception.
        dead = 4_194_303  # above the default pid_max
        self.assertFalse(proc.exists(dead))
        self.assertIsNone(proc.read_stat(dead))
        self.assertIsNone(proc.read_comm(dead))
        self.assertEqual(proc.read_cmdline(dead), [])
        self.assertIsNone(proc.read_exe(dead))
        self.assertEqual(proc.thread_ids(dead), [])
        self.assertEqual(proc.thread_count(dead), 0)
        self.assertEqual(proc.snapshot_threads(dead), {})
        self.assertIsNone(proc.read_schedstat(dead))

    def test_comm_with_spaces_and_parens_is_parsed(self):
        """The classic /proc/pid/stat trap: comm can contain ')' and spaces."""
        sample = (
            "1234 (weird ) name) S 1 1234 1234 0 -1 4194304 100 0 0 0 "
            + " ".join(str(i) for i in range(11, 30))
            + "\n"
        )
        original = proc._read_text
        proc._read_text = lambda _path: sample
        try:
            stat = proc.read_stat(1234)
        finally:
            proc._read_text = original
        self.assertIsNotNone(stat)
        self.assertEqual(stat.comm, "weird ) name")

    def test_system_info_is_schema_shaped(self):
        info = proc.system_info(os.getpid())
        self.assertEqual(info["schema_version"], layout.SCHEMA_VERSION)
        self.assertTrue(info["kernel"])
        self.assertGreaterEqual(info["cpu_count"], 1)
        self.assertIn("cgroup", info)

    def test_capabilities_reports_root_honestly(self):
        ok, caps = proc.have_capabilities()
        self.assertEqual(caps["root"], os.geteuid() == 0)
        if caps["root"]:
            self.assertTrue(ok)


class CpuTests(unittest.TestCase):
    def test_sample_cpu_measures_a_busy_child(self):
        sample = proc.sample_cpu(os.getpid(), 0.2)
        self.assertIsNotNone(sample)
        self.assertGreaterEqual(sample["cpu_pct"], 0.0)
        self.assertGreater(sample["window_s"], 0.0)

    def test_cpu_pct_between(self):
        ticks = proc.CLK_TCK
        self.assertEqual(proc.cpu_pct_between((0, 0), (ticks, 0), 1.0), 100.0)
        self.assertEqual(proc.cpu_pct_between((0, 0), (ticks, ticks), 2.0), 100.0)
        self.assertIsNone(proc.cpu_pct_between(None, (1, 1), 1.0))
        self.assertIsNone(proc.cpu_pct_between((0, 0), (1, 1), 0))

    def test_overhead_is_relative_to_the_baseline(self):
        self.assertEqual(proc.overhead_pct(100.0, 110.0), 10.0)
        self.assertEqual(proc.overhead_pct(200.0, 260.0), 30.0)

    def test_an_idle_baseline_does_not_inflate_overhead(self):
        """0.7% -> 1.0% of a CPU is tick noise, not a 43% slowdown."""
        self.assertLess(proc.overhead_pct(0.7, 1.0), 5.0)
        # A genuine cost on an idle target still shows.
        self.assertEqual(proc.overhead_pct(1.0, 6.0), 50.0)

    def test_overhead_never_goes_negative(self):
        # Tracing cannot make the target cheaper; a negative delta is noise.
        self.assertEqual(proc.overhead_pct(100.0, 90.0), 0.0)

    def test_overhead_without_samples_is_zero(self):
        self.assertEqual(proc.overhead_pct(None, 50.0), 0.0)
        self.assertEqual(proc.overhead_pct(50.0, None), 0.0)
        self.assertEqual(proc.overhead_pct(0.0, 50.0), 0.0)

    def test_baseline_ignores_missing_samples(self):
        self.assertEqual(proc.baseline_cpu_pct([{"cpu_pct": 10.0}, None]), 10.0)
        self.assertIsNone(proc.baseline_cpu_pct([None, {}]))

    def test_baseline_takes_the_higher_untraced_sample(self):
        """A disturbed baseline reads low, and would inflate the estimate."""
        self.assertEqual(
            proc.baseline_cpu_pct([{"cpu_pct": 140.0}, {"cpu_pct": 61.0}]), 140.0
        )

    def test_baseline_disagreement_is_measurable(self):
        self.assertIsNone(proc.baseline_disagreement([{"cpu_pct": 10.0}]))
        self.assertAlmostEqual(
            proc.baseline_disagreement([{"cpu_pct": 140.0}, {"cpu_pct": 70.0}]), 0.5
        )
        self.assertAlmostEqual(
            proc.baseline_disagreement([{"cpu_pct": 100.0}, {"cpu_pct": 100.0}]), 0.0
        )


class ChildTests(unittest.TestCase):
    def setUp(self):
        self.child = python_sleeper(30)
        self.addCleanup(self.child.wait)
        self.addCleanup(reap, self.child)

    def test_children_are_listed(self):
        self.assertIn(self.child.pid, proc.child_pids(os.getpid()))
        self.assertEqual(proc.read_stat(self.child.pid).ppid, os.getpid())

    def test_children_are_found_without_the_children_file(self):
        """Kernels built without CONFIG_PROC_CHILDREN have no such file."""
        original = proc._read_text

        def without_children(path):
            return None if path.name == "children" else original(path)

        with mock.patch.object(proc, "_read_text", side_effect=without_children):
            self.assertIn(self.child.pid, proc.child_pids(os.getpid()))


class PidRecyclingTests(unittest.TestCase):
    def test_same_process_detects_a_changed_start_time(self):
        pid = os.getpid()
        stat = proc.read_stat(pid)
        self.assertTrue(proc.is_same_process(pid, stat.start_time_ticks))
        # A different start time means the pid was recycled onto another
        # process, which must not be measured as if it were the target.
        self.assertFalse(proc.is_same_process(pid, stat.start_time_ticks + 1))

    def test_dead_pid_is_not_the_same_process(self):
        child = python_sleeper(0.01)
        child.wait()
        self.assertFalse(proc.is_same_process(child.pid, 12345))


@requires_target
class RealTargetTests(unittest.TestCase):
    """Against a process with a few hundred real threads."""

    @classmethod
    def setUpClass(cls):
        cls._ctx = spawn_target(threads=64, seconds=120, sleep_us=2000)
        cls.target = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_thread_count_matches_the_workload(self):
        # 64 workers plus the main thread.
        self.assertEqual(proc.thread_count(self.target.pid), 65)
        self.assertEqual(len(proc.thread_ids(self.target.pid)), 65)

    def test_thread_names_are_readable(self):
        snapshot = proc.snapshot_threads(self.target.pid)
        self.assertEqual(len(snapshot), 65)
        names = {entry["name"] for entry in snapshot.values()}
        self.assertIn("worker0", names)
        self.assertIn("contention", names)  # the main thread

    def test_schedstat_is_aggregated_over_all_threads(self):
        """The process level file covers only the main thread."""
        main_only = proc.read_schedstat(self.target.pid)
        aggregated = proc.aggregate_schedstat(self.target.pid)
        if main_only is None:
            self.skipTest("CONFIG_SCHEDSTATS is not enabled on this kernel")
        self.assertGreater(aggregated.run_ns, main_only.run_ns)

    def test_context_switches_accumulate(self):
        first = proc.aggregate_ctxt_switches(self.target.pid)
        self.assertTrue(any(v > 0 for v in first))

    def test_snapshot_merge_marks_arrivals_and_departures(self):
        start = {1: {"name": "a", "schedstat": None}, 2: {"name": "b", "schedstat": None}}
        end = {2: {"name": "b", "schedstat": None}, 3: {"name": "c", "schedstat": None}}
        merged = proc.merge_thread_snapshots(start, end)
        self.assertEqual(set(merged), {"1", "2", "3"})
        self.assertTrue(merged["1"]["exited"])
        self.assertEqual(merged["1"]["first_seen"], "start")
        self.assertEqual(merged["3"]["first_seen"], "end")
        self.assertNotIn("exited", merged["2"])

    def test_snapshot_merge_does_not_subtract_recycled_tid(self):
        start = {7: {"name": "old", "start_time_ticks": 100, "schedstat": {"run_ns": 900}}}
        end = {7: {"name": "new", "start_time_ticks": 200, "schedstat": {"run_ns": 40}}}
        entry = proc.merge_thread_snapshots(start, end)["7"]
        self.assertEqual(entry["start_time_ticks"], 200)
        self.assertEqual(entry["first_seen"], "end")
        self.assertNotIn("start_schedstat", entry)

    def test_target_info_carries_what_probes_need(self):
        info = proc.target_info(self.target.pid)
        self.assertEqual(info["pid"], self.target.pid)
        self.assertEqual(info["comm"], "contention")
        self.assertTrue(info["exe"].endswith("contention"))
        self.assertIn("--threads", info["cmdline"])
        self.assertIsNotNone(info["start_time_ticks"])


if __name__ == "__main__":
    unittest.main()
