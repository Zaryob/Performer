"""Offline reference comparison: preserve evidence and reject false matches."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

from performer import layout
from performer.bundle import Bundle, BundleBuilder
from performer.manifest import ProbeResult, Quality, TargetInfo, build_manifest
from tests import perf_compare

START = dt.datetime(2026, 10, 4, 10, tzinfo=dt.timezone.utc)
TEXT = """worker pool 123/123 100.100000: cpu-clock:
        0000000000400123 work(unsigned long)+0x10 (/tmp/my app)
        0000000000400200 main+0x20 (/tmp/my app)

worker pool 123/124 100.200000: cpu-clock:
        0000000000abcdef [unknown] (/tmp/my app)
        0000000000400200 main+0x20 (/tmp/my app)

worker pool 123/124 100.300000: cpu-clock:
        0000000000400200 main+0x20 (/tmp/my app)
"""


class PerfParseTests(unittest.TestCase):
    def test_preserves_comm_spaces_address_offset_and_module(self):
        samples, warnings = perf_compare.parse_perf(TEXT)
        self.assertEqual(warnings, [])
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0].comm, "worker pool")
        frame = samples[0].frames[0]
        self.assertEqual(frame.address, "0000000000400123")
        self.assertEqual(frame.symbol, "work(unsigned long)+0x10")
        self.assertEqual(frame.module, "/tmp/my app")
        self.assertTrue(samples[1].frames[0].unknown)

    def test_optional_cpu_period_and_event_scope(self):
        samples, warnings = perf_compare.parse_perf("app 123/124 [002] 100.1: 42 cpu-clock:u:\n")
        self.assertEqual(warnings, [])
        self.assertEqual(samples[0].event, "cpu-clock:u")
        self.assertEqual(samples[0].tid, 124)

    def test_inline_ip_is_not_counted_twice(self):
        samples, warnings = perf_compare.parse_perf(
            "app 123/124 100.1: cpu-clock: 400123 work+0x1 (/tmp/app)\n"
            "        400123 work+0x1 (/tmp/app)\n"
            "        400200 main+0x2 (/tmp/app)\n"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(samples[0].frames), 2)

    def test_different_inline_frame_and_recursive_frames_survive(self):
        samples, _ = perf_compare.parse_perf(
            "app 123/124 100.1: cpu-clock: 400123 work+0x1 (/tmp/app)\n"
            "        400200 recursive+0x2 (/tmp/app)\n"
            "        400200 recursive+0x2 (/tmp/app)\n"
        )
        self.assertEqual(len(samples[0].frames), 3)

    def test_unrecognised_record_does_not_donate_frames_to_previous_sample(self):
        samples, warnings = perf_compare.parse_perf(
            "app 123/124 100.1: cpu-clock:\n"
            "garbled new event\n"
            "        400200 main (/tmp/app)\n"
        )
        self.assertEqual(samples[0].frames, [])
        self.assertEqual(len(warnings), 2)

    def test_weighted_folded_frames_exclude_thread_root(self):
        counts, total, unassigned, quality = perf_compare.summarise_folded([
            ("worker [tid=123];work;[unknown]", 3),
            ("worker [tid=124];[unknown]_[k]", 2),
            ("legacy;work", 1),
        ])
        self.assertEqual(dict(counts), {123: 3, 124: 2})
        self.assertEqual((total, unassigned), (6, 1))
        self.assertEqual(quality["sampled_frame_occurrences"], 9)
        self.assertEqual(quality["unknown_frame_occurrences"], 5)
        self.assertAlmostEqual(quality["unknown_frame_ratio"], 5 / 9)

    def test_empty_callchain_has_no_invented_unknown_frame(self):
        quality = perf_compare.frame_quality(0, 0, 2)
        self.assertIsNone(quality["unknown_frame_ratio"])
        self.assertEqual(quality["samples_without_frames"], 2)

    def test_cpu_delta_keeps_true_zero_but_not_missing_or_decreasing(self):
        self.assertEqual(perf_compare.cpu_delta_ms({"start_schedstat": {"run_ns": 0},
                                                   "end_schedstat": {"run_ns": 0}}), 0)
        for entry in ({}, {"start_schedstat": {"run_ns": 1}},
                      {"start_schedstat": {"run_ns": 2}, "end_schedstat": {"run_ns": 1}},
                      {"start_schedstat": {"run_ns": 0}, "end_schedstat": {"run_ns": float("nan")}},
                      {"start_schedstat": {"run_ns": False}, "end_schedstat": {"run_ns": 1}}):
            self.assertIsNone(perf_compare.cpu_delta_ms(entry))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.builder = BundleBuilder(self.path, label="compare", started_at=START)
        self.builder.add_folded(layout.STACK_ONCPU, [
            ("worker [tid=123];work;[unknown]", 3),
            ("worker [tid=125];work", 2),
        ])
        self.builder.add_folded(layout.STACK_OFFCPU, [("worker [tid=126];wait", 2000)])
        self.builder.add_json(layout.META_THREADS, {"schema_version": 2, "threads": {
            "123": {"name": "worker", "start_schedstat": {"run_ns": 0, "wait_ns": 0, "timeslices": 0},
                    "end_schedstat": {"run_ns": 5000000, "wait_ns": 0, "timeslices": 0}},
            "125": {"name": "worker"}, "127": {"name": "idle"},
        }})
        self.document = build_manifest(
            label="compare", profile="standard", duration_s=20, started_at=START,
            ended_at=START + dt.timedelta(seconds=20), actual_duration_s=20,
            target=TargetInfo(pid=123, comm="app", thread_count_start=3, thread_count_end=3),
            probes=[ProbeResult(name="oncpu", status="ok", outputs=[layout.STACK_ONCPU],
                                thresholds={"sample_hz": 99}),
                    ProbeResult(name="offcpu", status="ok", outputs=[layout.STACK_OFFCPU],
                                warnings=["1 right-censored wait"])],
            quality=Quality(), tool_versions={"performer": "0.1.0"},
        )
        self.builder.write_manifest(self.document)
        self.metadata = {"pid": 123, "event": "cpu-clock", "sample_hz": 99,
                         "call_graph": "fp", "clock": "boottime",
                         "started_at": "2026-10-04T10:00:00Z", "ended_at": "2026-10-04T10:00:20Z"}

    def report(self, text=TEXT, meta=None):
        with Bundle.open(self.builder.root) as bundle:
            return perf_compare.compare(bundle, text, self.metadata if meta is None else meta)

    def checks(self, report):
        return {item["name"]: item for item in report["comparability"]["checks"]}

    def add_gate(self):
        self.builder.add_json(layout.META_WINDOW, {"clock": "boottime", "start_ns": 100150000000,
                                                 "end_ns": 100250000000})
        self.builder.write_manifest(self.document)

    def test_union_of_same_named_inventory_and_observed_tids_is_complete(self):
        report = self.report()
        rows = {row["tid"]: row for row in report["threads"]}
        self.assertEqual(set(rows), {123, 124, 125, 126, 127})
        self.assertEqual(rows[123]["proc_cpu_ms"], 5)
        self.assertIsNone(rows[125]["proc_cpu_ms"])
        self.assertEqual(rows[124]["perf_oncpu_samples"], 2)
        self.assertTrue(rows[124]["only_perf_samples"])
        self.assertTrue(rows[125]["only_performer_samples"])
        self.assertEqual(rows[126]["performer_offcpu_us"], 2000)
        self.assertEqual(rows[127]["performer_oncpu_samples"], 0)
        self.assertEqual(report["performer"]["offcpu_warnings"], ["1 right-censored wait"])
        self.assertEqual(report["perf"]["quality"]["unknown_frame_occurrences"], 1)
        self.assertEqual(report["perf"]["quality"]["sampled_frame_occurrences"], 5)
        self.assertEqual(report["perf"]["unresolved_frames"][0]["address"], "0000000000abcdef")

    def test_other_pids_and_cycles_are_excluded_and_flagged(self):
        text = TEXT + "app 999/999 100.5: cpu-clock:\napp 123/123 100.6: cycles:\n"
        report = self.report(text)
        self.assertEqual(report["perf"]["target_cpu_clock_samples"], 3)
        self.assertEqual(report["perf"]["excluded_samples"], 2)
        self.assertEqual(self.checks(report)["pid"]["status"], "mismatch")

    def test_missing_metadata_does_not_claim_comparability(self):
        checks = self.checks(self.report(meta={}))
        self.assertEqual(checks["recording_window"]["status"], "unknown")
        self.assertEqual(checks["sample_hz"]["status"], "unknown")
        self.assertEqual(checks["unwinder"]["status"], "unknown")

    def test_rate_scope_and_duration_mismatches_are_explicit(self):
        meta = dict(self.metadata, sample_hz=999, event="cpu-clock:u", ended_at="2026-10-04T10:00:10Z")
        checks = self.checks(self.report(meta=meta))
        for name in ("sample_hz", "event", "recording_window"):
            self.assertEqual(checks[name]["status"], "mismatch")

    def test_dwarf_is_reported_as_alternative_unwinder(self):
        checks = self.checks(self.report(meta=dict(self.metadata, call_graph="dwarf,16384")))
        self.assertEqual(checks["unwinder"]["status"], "advisory")

    def test_simultaneous_reference_does_not_certify_collector_overhead(self):
        report = self.report(meta=dict(self.metadata, simultaneous_recording=True))
        self.assertEqual(self.checks(report)["overhead"]["status"], "advisory")
        self.assertIn("separate Performer-only", self.checks(report)["overhead"]["detail"])

    def test_shared_boottime_clock_filters_to_actual_gate(self):
        self.add_gate()
        meta = dict(self.metadata, started_at="2026-10-04T09:59:50Z", ended_at="2026-10-04T10:00:30Z")
        report = self.report(meta=meta)
        self.assertEqual(report["perf"]["target_cpu_clock_samples"], 1)
        self.assertEqual(report["comparison_window"]["mode"], "performer_gate")
        self.assertEqual(report["comparison_window"]["samples_outside_window"], 2)
        self.assertTrue(report["recording_window"]["capture_windows_differ"])
        self.assertEqual(self.checks(report)["recording_window"]["status"], "matched")
        self.assertEqual(self.checks(report)["perf_clock"]["status"], "matched")
        self.assertEqual(report["perf"]["quality"]["sampled_frame_occurrences"], 2)

    def test_gate_is_half_open_and_nanoseconds_are_not_rounded_to_floats(self):
        self.add_gate()
        text = """app 123/123 100.149999999: cpu-clock:
app 123/123 100.150000000: cpu-clock:
app 123/123 100.249999999: cpu-clock:
app 123/123 100.250000000: cpu-clock:
"""
        report = self.report(text=text)
        self.assertEqual(report["perf"]["target_cpu_clock_samples"], 2)
        self.assertEqual(report["perf"]["sample_time_first_ns"], 100150000000)
        self.assertEqual(report["perf"]["sample_time_last_ns"], 100249999999)

    def test_mismatched_clock_does_not_silently_clip_samples(self):
        self.add_gate()
        report = self.report(meta=dict(self.metadata, clock="monotonic"))
        self.assertEqual(report["perf"]["target_cpu_clock_samples"], 3)
        self.assertEqual(report["comparison_window"]["mode"], "untrimmed")
        self.assertEqual(self.checks(report)["perf_clock"]["status"], "mismatch")

    def test_supplied_monotonic_calibration_is_explicit(self):
        self.add_gate()
        report = self.report(meta=dict(self.metadata, clock="monotonic", boottime_minus_monotonic_s=0))
        self.assertEqual(report["perf"]["target_cpu_clock_samples"], 1)
        self.assertEqual(report["comparison_window"]["boottime_minus_monotonic_s"], 0)
        self.assertEqual(self.checks(report)["perf_clock"]["status"], "advisory")

    def test_clipping_does_not_certify_reference_that_misses_end_of_window(self):
        self.add_gate()
        report = self.report(meta=dict(self.metadata, ended_at="2026-10-04T10:00:10Z"))
        self.assertEqual(self.checks(report)["recording_window"]["status"], "mismatch")

    def test_cli_writes_evidence_artifact(self):
        script = self.path / "reference.txt"
        script.write_text(TEXT)
        metadata = self.path / "metadata.json"
        metadata.write_text(json.dumps(self.metadata))
        result = self.path / "comparison.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = perf_compare.main([str(self.builder.root), str(script), "--perf-meta", str(metadata), "--json", str(result)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result.read_text())["perf"]["target_cpu_clock_samples"], 3)

    def test_cli_reports_bad_metadata_and_invalid_tolerance(self):
        script = self.path / "reference.txt"
        script.write_text(TEXT)
        metadata = self.path / "bad.json"
        metadata.write_text("[]")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = perf_compare.main([str(self.builder.root), str(script), "--perf-meta", str(metadata)])
        self.assertEqual(code, 2)
        self.assertIn("JSON object", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            code = perf_compare.main([str(self.builder.root), str(script), "--window-tolerance-s", "nan"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
