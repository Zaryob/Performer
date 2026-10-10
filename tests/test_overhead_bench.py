"""Overhead benchmark analysis: exact windows, honest gaps, visible failures."""

from __future__ import annotations

import unittest

from tests import overhead_bench as bench

S = 1_000_000_000


class WindowMetricsTests(unittest.TestCase):
    def test_ticks_are_parsed_and_noise_ignored(self):
        ticks = bench.parse_ticks(["pid 1 threads 2", "ready", "tick 10 5 7", "tick x 1 2", "iterations 9"])
        self.assertEqual(ticks, [(10, 5, 7)])

    def test_window_interpolates_between_ticks(self):
        ticks = [(0, 0, 0), (S, 1000, S // 2), (2 * S, 2000, S)]
        m = bench.window_metrics(ticks, S // 2, 3 * S // 2)
        self.assertAlmostEqual(m["window_s"], 1.0)
        self.assertAlmostEqual(m["iterations"], 1000.0)
        self.assertAlmostEqual(m["throughput_per_s"], 1000.0)
        self.assertAlmostEqual(m["cpu_cores"], 0.5)
        self.assertAlmostEqual(m["cpu_us_per_iteration"], 500.0)

    def test_window_outside_the_ticks_is_unknown_not_extrapolated(self):
        ticks = [(S, 0, 0), (2 * S, 100, 0)]
        self.assertIsNone(bench.window_metrics(ticks, 0, 2 * S))
        self.assertIsNone(bench.window_metrics(ticks, S, 3 * S))
        self.assertIsNone(bench.window_metrics(ticks, 2 * S, S))

    def test_window_without_iterations_has_no_cost_per_iteration(self):
        ticks = [(0, 7, 0), (S, 7, S)]
        self.assertIsNone(bench.window_metrics(ticks, 0, S)["cpu_us_per_iteration"])


def run(workload, mode, rnd, tput=None, cpu=None, error=None, lost=None):
    doc = {"workload": workload, "mode": mode, "round": rnd}
    if error:
        doc["error"] = error
    else:
        doc["metrics"] = {"throughput_per_s": tput, "cpu_us_per_iteration": cpu}
    if lost is not None:
        doc["events_lost"] = lost
    return doc


class SummaryTests(unittest.TestCase):
    def test_change_is_relative_to_the_baseline_median(self):
        results = {"runs": [
            run("cpu", "baseline", 0, 100, 10), run("cpu", "baseline", 1, 104, 10),
            run("cpu", "baseline", 2, 96, 10),
            run("cpu", "performer", 0, 95, 11, lost=0), run("cpu", "performer", 1, 97, 11, lost=2),
        ]}
        rows = bench.summarise(results)["cpu"]
        self.assertNotIn("throughput_change_pct", rows["baseline"])
        self.assertAlmostEqual(rows["performer"]["throughput_change_pct"], -4.0)
        self.assertAlmostEqual(rows["performer"]["cpu_per_iteration_change_pct"], 10.0)
        self.assertEqual(rows["performer"]["events_lost_total"], 2)
        self.assertEqual(rows["baseline"]["throughput"]["n"], 3)

    def test_self_estimates_are_summarised_with_missing_ones_counted(self):
        a = run("lock", "performer", 0, 9, 1)
        a["self_estimated_overhead_pct"] = 10.0
        b = run("lock", "performer", 1, 9, 1)
        b["self_estimated_overhead_pct"] = None
        results = {"runs": [run("lock", "baseline", 0, 10, 1), a, b]}
        summary = bench.summarise(results)
        row = summary["lock"]["performer"]
        self.assertEqual(row["self_estimate_pct"]["median"], 10.0)
        self.assertEqual(row["self_estimate_missing"], 1)
        self.assertIn("| 10.0% (1) |", bench.markdown(results, summary))
        self.assertTrue(bench.markdown(results, summary).splitlines()[2].endswith("| — |"))

    def test_failed_runs_are_reported_not_dropped(self):
        results = {"runs": [
            run("io", "baseline", 0, 50, 1),
            run("io", "performer", 0, error="probe offcpu failed"),
        ]}
        summary = bench.summarise(results)
        row = summary["io"]["performer"]
        self.assertIsNone(row["throughput"])
        self.assertNotIn("throughput_change_pct", row)
        self.assertEqual(row["failed_runs"], [{"round": 0, "error": "probe offcpu failed"}])
        table = bench.markdown(results, summary)
        self.assertIn("| io | performer | 0/1 |", table)
        self.assertIn("io/performer round 0: probe offcpu failed", table)

    def test_unknown_loss_is_not_shown_as_zero(self):
        results = {"runs": [run("lock", "baseline", 0, 10, 1), run("lock", "perf", 0, 9, 1)]}
        summary = bench.summarise(results)
        self.assertIsNone(summary["lock"]["perf"]["events_lost_total"])
        self.assertIn("| — |", bench.markdown(results, summary).splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
