"""Module tracing selection, cross-context correlation output and loss flags."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from performer import emit, kernel, layout, profiles, preflight, window
from performer.bundle import BundleBuilder
from performer.errors import PerformerError
from performer.jsonschema import load_schema

CONFIG = {"module": "demo_driver", "functions": ["demo_submit", "demo_work"],
          "tracepoints": ["demo_driver:queued"], "requests": [
              {"name": "io", "start": "demo_driver:submit", "end": "demo_driver:done", "key": "request_id"}]}


class KernelConfigTests(unittest.TestCase):
    def test_rejects_code_injection_and_unbounded_or_ambiguous_selection(self):
        for document in [None, {}, {"module": "demo", "functions": ["*"]},
                         {"module": "demo;exit()", "functions": ["fn"]},
                         {"module": "demo", "functions": ["fn"] * 33},
                         {"module": "demo", "functions": ["fn", "fn"]},
                         {"module": "demo", "tracepoints": ["group:*"]},
                         {"module": "demo", "source": "kprobe:fn {}"},
                         {"module": "demo", "requests": [{"name": "io", "start": "g:s", "end": "g:s", "key": "id"}]}]:
            with self.subTest(document=document), self.assertRaises(PerformerError):
                kernel.validate_config(document)

    def test_unique_request_endpoints_cannot_be_double_counted(self):
        config = dict(CONFIG, tracepoints=["demo_driver:submit"])
        with self.assertRaisesRegex(PerformerError, "unique"):
            kernel.validate_config(config)

    def test_common_tracepoint_header_fields_are_not_request_keys(self):
        config = dict(CONFIG, requests=[dict(CONFIG["requests"][0], key="common_pid")])
        with self.assertRaisesRegex(PerformerError, "event-specific"):
            kernel.validate_config(config)

    def test_host_checks_symbol_owner_and_scalar_tracepoint_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module/demo_driver").mkdir(parents=True)
            symbols = root / "kallsyms"
            symbols.write_text("0000 t demo_submit [demo_driver]\n0001 t demo_work [demo_driver]\n")
            for event in ("queued", "submit", "done"):
                target = root / "tracing/events/demo_driver" / event
                target.mkdir(parents=True)
                (target / "format").write_text("field:unsigned long request_id; offset:8; size:8; signed:0;\n")
            kwargs = dict(sys_module=root / "module", kallsyms=symbols, tracefs=root / "tracing")
            kernel.check_host(CONFIG, **kwargs)
            symbols.write_text(symbols.read_text() + "0002 t demo_submit [another_module]\n")
            with self.assertRaisesRegex(PerformerError, "uniquely belong"):
                kernel.check_host(CONFIG, **kwargs)
            symbols.write_text("0000 t demo_submit [demo_driver]\n0001 t demo_work [demo_driver]\n")
            (root / "tracing/events/demo_driver/done/format").write_text("field:char * request_id; offset:8; size:8;\n")
            with self.assertRaisesRegex(PerformerError, "scalar integer"):
                kernel.check_host(CONFIG, **kwargs)

    def test_missing_module_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(PerformerError, "not loaded"):
            kernel.check_host(CONFIG, sys_module=Path(tmp))

    def test_generated_module_probe_is_system_wide_and_014_compatible(self):
        config = kernel.validate_config(CONFIG)
        source = kernel.render(config)
        self.assertIn("kprobe:demo_work", source)
        self.assertNotIn("kprobe:demo_driver:", source)
        self.assertNotIn("pid == $1", source)
        self.assertNotIn("BEGIN", source)
        self.assertNotIn("END", source)
        self.assertIn("@_kernel_request_start[0, $id]", source)
        self.assertIn("$ctx = -1 - (int64)cpu", source)
        self.assertIn("$depth <= 16", source)
        gated = window.render_program(source, 100, control_token=42)
        self.assertIn("@_performer_start", gated)
        self.assertNotIn("nsecs - $start", gated)
        self.assertIn("$__performer_now - $start", gated)
        with tempfile.TemporaryDirectory() as tmp:
            profile = kernel.extend_profile(profiles.load("light"), config)
            spec = profile.probe("kernel")
            command = profiles.probe_command(spec, 123, 60, bpftrace="bpftrace", generated_dir=Path(tmp), window_control_pid=100)
            self.assertEqual(Path(command[3]).read_text(), window.render_program(source, 100))
            self.assertEqual(preflight.bpftrace_env(spec)["BPFTRACE_MAP_KEYS_MAX"], "8192")
            self.assertTrue(preflight.check_probe_programs(profile).ok)

    def test_kernel_only_event_trials_can_be_empty_without_disabling_probe(self):
        profile = kernel.extend_profile(profiles.load("light"), kernel.validate_config(CONFIG))
        report = preflight.PreflightReport()
        trial = preflight.TrialResult(ran=True, stdout="Attaching 3 probes...\n", stderr="", exit_reason="sigint", exit_code=0)
        with patch("performer.preflight.run_trial", return_value=trial):
            preflight.check_smoke(report, profile, 123)
        self.assertTrue(report.smoke["kernel"])
        self.assertFalse(report.smoke_data["kernel"])


class KernelEmissionTests(unittest.TestCase):
    def test_request_ids_and_nanoseconds_remain_exact_and_no_pid_attribution_is_invented(self):
        text = '''@_performer_start: 10000000000000000
@kernel_calls[demo_work]: 4
@kernel_function_ns[demo_work]: count 3, average 2000, total 6000
@kernel_function_max_ns[demo_work]: 3000
@kernel_request_started[io]: 2
@kernel_request_ns[io]: count 1, average 12000, total 12000
@kernel_request_max_ns[io]: 12000
@kernel_request_unmatched[io]: 1
@kernel_event[10000000000000001, io, 1, 18446744073709551615, 100, 101, 0]: 1
@kernel_event[10000000000012001, io, 2, 18446744073709551615, 0, 22, 1]: 1
'''
        with tempfile.TemporaryDirectory() as tmp:
            builder = BundleBuilder(Path(tmp), label="kernel")
            result = emit.emit("kernel", emit.EmitContext(builder, 1), text)
            self.assertFalse(result.partial)
            requests = json.loads(builder.path_for(layout.HIST_KERNEL_REQUESTS).read_text())
            self.assertEqual(requests["rows"], [["io", 2, 1, 12.0, 12.0, 12.0, 1, 1, 0]])
            events = json.loads(builder.path_for(layout.HIST_KERNEL_EVENTS).read_text())
            self.assertEqual(events["rows"][0][0], "10000000000000001")
            self.assertEqual(events["rows"][1][4], "18446744073709551615")
            self.assertEqual(events["rows"][1][5:7], [0, 22])
            self.assertAlmostEqual(events["rows"][1][1], 0.012001)
            for path in result.outputs:
                doc = json.loads(builder.path_for(path).read_text())
                errors = list(load_schema(layout.schema_path("table.schema.json")).validate(doc))
                self.assertEqual(errors, [], path)

    def test_loss_and_request_key_reuse_mark_data_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BundleBuilder(Path(tmp), label="loss")
            result = emit.emit("kernel", emit.EmitContext(builder, 1), "@kernel_events_dropped: 12\n@kernel_request_reused[io]: 2\n")
            self.assertTrue(result.partial)
            self.assertTrue(any("12 kernel events dropped" in warning for warning in result.warnings))
            self.assertTrue(any("IDs reused" in warning for warning in result.warnings))
            self.assertTrue(json.loads(builder.path_for(layout.HIST_KERNEL_EVENTS).read_text())["truncated"])

    def test_healthy_idle_capture_has_empty_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BundleBuilder(Path(tmp), label="idle")
            result = emit.emit("kernel", emit.EmitContext(builder, 1), "@_performer_start: 100\n")
            self.assertFalse(result.partial)
            self.assertEqual(len(result.outputs), 4)

    def test_missing_event_map_entries_mark_data_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BundleBuilder(Path(tmp), label="map-loss")
            result = emit.emit("kernel", emit.EmitContext(builder, 1), "@_performer_start: 100\n@_kernel_event_count[0]: 2\n")
            self.assertTrue(result.partial)
            self.assertTrue(any("some records are missing" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
