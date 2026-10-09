"""Collection boundaries must be proven before accepting measured events."""

from __future__ import annotations

import ctypes
import errno
import unittest
from types import SimpleNamespace
from unittest import mock

from performer import profiles, window
from performer.errors import PerformerError


class RenderWindowTests(unittest.TestCase):
    def test_every_shipped_program_keeps_readiness_but_gates_measurements(self):
        for path in sorted(profiles.probes_dir().glob("*.bt")):
            with self.subTest(program=path.name):
                source = path.read_text(encoding="utf-8")
                rendered = window.render_program(source, 9876, offcpu=path.name == "offcpu.bt")
                original_mask = window._code_mask(source)
                measured = sum(
                    point != "interval:"
                    for point in window._ATTACHPOINT_RE.findall(original_mask)
                )
                # Multiple attachpoints sharing a body need only one gate.
                self.assertGreater(measured, 0)
                self.assertGreater(rendered.count("$__performer_now = nsecs;"), 0)
                self.assertIn('printf("PERFORMER_READY\\n")', rendered)
                ready = rendered.index('printf("PERFORMER_READY\\n")')
                interval = rendered.rfind("interval:ms:100", 0, ready)
                self.assertNotIn("$__performer_now", rendered[interval:ready])
                self.assertIn("interval:s:$2\n{\n    exit();\n}", rendered)
                self.assertIn("pid == 9876", rendered)
                self.assertIn("PERFORMER_WINDOW_SEALED", rendered)
                self.assertEqual(rendered.count("@_performer_observed_end = max($__performer_witness);"), 2)
                self.assertNotIn("BEGIN\n", rendered)
                self.assertNotIn("END\n", rendered)

    def test_comments_and_string_braces_do_not_escape_or_hide_a_gate(self):
        source = '''/* profile:hz:99 { pretend } */
tracepoint:example:event / pid == $1 /
{
    // interval:ms:100 { pretend }
    if (tid) { printf("literal } { \\" text\\n"); }
    @events = count();
}
interval:s:$2 { exit(); }
'''
        rendered = window.render_program(source, 42)
        self.assertEqual(rendered.count("$__performer_now = nsecs;"), 1)
        self.assertIn("if (tid)", rendered)
        self.assertIn("$__performer_now < @_performer_end", rendered)
        self.assertIn("interval:s:$2 { exit(); }", rendered)

    def test_existing_predicates_and_shared_attachpoints_stay_intact(self):
        source = "tracepoint:x:one,\ntracepoint:x:two\n/ tid == $1 /\n{ @n = count(); }\n"
        rendered = window.render_program(source, 42, control_token=17)
        self.assertIn("tracepoint:x:one,\ntracepoint:x:two\n/ tid == $1 /", rendered)
        self.assertEqual(rendered.count("$__performer_now = nsecs;"), 1)
        self.assertIn("(uint64)args->arg4 == 17", rendered)

    def test_gate_and_duration_math_use_one_event_timestamp(self):
        source = '''tracepoint:x:event {
    @_start[tid] = nsecs;
    @duration = sum(nsecs - @_start[tid]);
    printf("nsecs remains literal\\n");
    // nsecs remains a comment
}
'''
        rendered = window.render_program(source, 42)
        measured = rendered[rendered.index("// Measured probe handlers"):]
        self.assertIn("@_start[tid] = $__performer_now;", measured)
        self.assertIn("sum($__performer_now - @_start[tid])", measured)
        self.assertIn('printf("nsecs remains literal', measured)
        self.assertIn("// nsecs remains a comment", measured)
        handler = measured[:measured.index("interval:ms:100")]
        self.assertEqual(window._code_mask(handler).count("nsecs"), 1)

    def test_control_handler_has_no_collection_predicate(self):
        rendered = window.render_program("profile:hz:99 { @n = count(); }", 42)
        control = rendered[rendered.index("tracepoint:syscalls:sys_enter_prctl"):rendered.index("// Measured probe handlers")]
        self.assertNotIn("$__performer_now", control)
        self.assertIn("@_performer_start = (uint64)args->arg2;", control)
        self.assertIn("@_performer_end = (uint64)args->arg3;", control)

    def test_offcpu_observed_clock_is_capped_and_can_be_sealed_after_end(self):
        source = (profiles.probes_dir() / "offcpu.bt").read_text(encoding="utf-8")
        rendered = window.render_program(source, 42, offcpu=True)
        self.assertNotIn("@_off_window_start = nsecs;", rendered)
        self.assertIn("@_off_window_start = @_performer_start;", rendered)
        self.assertIn("@_off_min_us = $3;", rendered)
        # Both the periodic witness and final control cap to the chosen end.
        self.assertEqual(rendered.count("@_off_window_end = @_performer_end;"), 4)
        self.assertEqual(rendered.count("if (@_performer_start && nsecs >= @_performer_start)"), 2)

    def test_partial_duration_has_a_periodic_witness_without_per_event_updates(self):
        rendered = window.render_program("profile:hz:99 { @n=count(); }", 42)
        measured, interval = rendered.split("// Measured probe handlers", 1)[1].split("interval:ms:100", 1)
        self.assertNotIn("@_performer_observed_end", measured)
        self.assertIn("@_performer_observed_end = max($__performer_witness);", interval)
        self.assertIn("if (@_performer_sealed && @_performer_end)", interval)

    def test_ambiguous_or_broken_programs_are_refused(self):
        bad_sources = (
            "tracepoint:x:y { @n=count();",
            "tracepoint:x:y { @n=count(); }}",
            "interval:ms:100 { @n=count(); }",
            "tracepoint:x:y, interval:ms:100 { @n=count(); }",
            "struct x { int a; }; profile:hz:99 { @n=count(); }",
            "/* unterminated",
            'profile:hz:99 { printf("unterminated); }',
        )
        for source in bad_sources:
            with self.subTest(source=source), self.assertRaises(PerformerError):
                window.render_program(source, 42)

    def test_double_gating_and_unknown_offcpu_layout_are_refused(self):
        source = "profile:hz:99 { @n=count(); }"
        rendered = window.render_program(source, 42)
        with self.assertRaisesRegex(PerformerError, "already gated"):
            window.render_program(rendered, 42)
        with self.assertRaisesRegex(PerformerError, "unrecognised tracing clock"):
            window.render_program(source, 42, offcpu=True)

    def test_invalid_control_identities_are_rejected(self):
        for pid in (0, -2, True, "42"):
            with self.subTest(pid=pid), self.assertRaises(PerformerError):
                window.render_program("profile:hz:99 { @n=count(); }", pid)
        with self.assertRaises(PerformerError):
            window.render_program("profile:hz:99 { @n=count(); }", 42, control_token=-1)


class SignalBoundsTests(unittest.TestCase):
    def test_uses_boottime_when_it_is_available(self):
        fake_time = SimpleNamespace(CLOCK_BOOTTIME=7, clock_gettime_ns=mock.Mock(return_value=123))
        with mock.patch.object(window, "time", fake_time):
            self.assertEqual(window.clock_now_ns(), 123)
        fake_time.clock_gettime_ns.assert_called_once_with(7)

    def test_development_machine_clock_fallback(self):
        fake_time = SimpleNamespace(monotonic_ns=mock.Mock(return_value=456))
        with mock.patch.object(window, "time", fake_time):
            self.assertEqual(window.clock_now_ns(), 456)

    def _control(self, error):
        def call(*_args):
            ctypes.set_errno(error)
            return -1
        return mock.Mock(side_effect=call)

    def test_prctl_preserves_full_width_bounds_and_run_token(self):
        control = self._control(errno.EINVAL)
        library = SimpleNamespace(prctl=control)
        start = (1 << 40) + 12
        end = start + 99
        with mock.patch.object(window.platform, "system", return_value="Linux"), \
                mock.patch.object(window.ctypes, "CDLL", return_value=library), \
                mock.patch.object(window, "clock_now_ns", side_effect=(start - 20, start - 10)):
            self.assertEqual(window.signal_bounds(start, end, control_token=43), (start - 20, start - 10))
        self.assertEqual([argument.value for argument in control.call_args.args], [window.CONTROL_OPTION, start, end, 43, 0])

    def test_a_blocked_control_syscall_does_not_start_collection(self):
        library = SimpleNamespace(prctl=self._control(errno.EPERM))
        with mock.patch.object(window.platform, "system", return_value="Linux"), \
                mock.patch.object(window.ctypes, "CDLL", return_value=library), \
                self.assertRaisesRegex(PerformerError, "could not signal"):
            window.signal_bounds(100, 200)

    def test_unexpected_success_is_not_assumed_to_be_side_effect_free(self):
        library = SimpleNamespace(prctl=mock.Mock(return_value=0))
        with mock.patch.object(window.platform, "system", return_value="Linux"), \
                mock.patch.object(window.ctypes, "CDLL", return_value=library), \
                self.assertRaises(PerformerError):
            window.signal_bounds(100, 200)

    def test_non_linux_fake_collection_needs_no_prctl_symbol(self):
        with mock.patch.object(window.platform, "system", return_value="Darwin"), \
                mock.patch.object(window.ctypes, "CDLL") as library, \
                mock.patch.object(window, "clock_now_ns", return_value=12):
            self.assertEqual(window.signal_bounds(100, 200), (12, 12))
        library.assert_not_called()

    def test_bad_bounds_never_reach_the_kernel(self):
        for start, end in ((0, 0), (100, 99), (100, 100), (-1, 200), (True, 200), (100, 1 << 64)):
            with self.subTest(start=start, end=end), \
                    mock.patch.object(window.ctypes, "CDLL") as library, \
                    self.assertRaises(PerformerError):
                window.signal_bounds(start, end)
            library.assert_not_called()

    def test_until_exit_can_be_armed_without_a_finite_end(self):
        with mock.patch.object(window.platform, "system", return_value="Darwin"):
            window.signal_bounds(100, 0)


class AcknowledgeWindowTests(unittest.TestCase):
    def _probe(self, output="", alive=True):
        return SimpleNamespace(name="oncpu", alive=alive, read_stdout=mock.Mock(return_value=output))

    def test_exact_bounds_are_required_before_the_deadline(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n")
        window.wait_for_ack([probe], 100, 200, 90, clock=lambda: 10)

    def test_stale_substring_and_map_values_are_not_acknowledgements(self):
        for output in ("PERFORMER_WINDOW 99 200\n", "@marker[PERFORMER_WINDOW 100 200]: 1\n", "prefix PERFORMER_WINDOW 100 200\n"):
            probe = self._probe(output)
            with self.subTest(output=output), self.assertRaisesRegex(PerformerError, "timed out"):
                window.wait_for_ack([probe], 100, 200, 90, clock=mock.Mock(side_effect=(10, 90)), sleep=lambda _seconds: None)

    def test_every_probe_must_acknowledge_the_same_bounds(self):
        first = self._probe("PERFORMER_WINDOW 100 200\n")
        second = self._probe("")
        second.name = "offcpu"
        def flush(_seconds):
            second.read_stdout.return_value = "PERFORMER_WINDOW 100 200\n"
        window.wait_for_ack([first, second], 100, 200, 90, clock=lambda: 10, sleep=flush)
        self.assertEqual(second.read_stdout.call_count, 2)

    def test_a_late_acknowledgement_cannot_certify_startup(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n")
        with self.assertRaisesRegex(PerformerError, "timed out"):
            window.wait_for_ack([probe], 100, 200, 90, clock=lambda: 90)

    def test_reading_a_marker_cannot_carry_the_barrier_past_its_deadline(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n")
        with self.assertRaisesRegex(PerformerError, "timed out"):
            window.wait_for_ack([probe], 100, 200, 90, clock=mock.Mock(side_effect=(10, 90)))

    def test_a_dead_probe_is_not_certified_by_its_old_marker(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n", alive=False)
        with self.assertRaisesRegex(PerformerError, "exited"):
            window.wait_for_ack([probe], 100, 200, 90, clock=lambda: 10)

    def test_sealing_requires_a_distinct_post_end_marker(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n")
        def seal(_seconds):
            probe.read_stdout.return_value += "PERFORMER_WINDOW_SEALED 100 200\n"
        window.wait_for_ack([probe], 100, 200, 300, sealed=True, clock=lambda: 250, sleep=seal)
        self.assertEqual(probe.read_stdout.call_count, 2)

    def test_matching_clock_witness_calibrates_the_control(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\nPERFORMER_CLOCK 15\n")
        window.wait_for_ack([probe], 100, 200, 90, control_window_ns=(10, 20), clock=lambda: 30)

    def test_a_foreign_clock_is_refused_even_with_exact_bounds(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\nPERFORMER_CLOCK 5\n")
        with self.assertRaisesRegex(PerformerError, "clock does not match"):
            window.wait_for_ack([probe], 100, 200, 90, control_window_ns=(10, 20), clock=lambda: 30)

    def test_a_pending_clock_line_is_waited_for(self):
        probe = self._probe("PERFORMER_WINDOW 100 200\n")
        def flush(_seconds):
            probe.read_stdout.return_value += "PERFORMER_CLOCK 15\n"
        window.wait_for_ack([probe], 100, 200, 90, control_window_ns=(10, 20), clock=lambda: 30, sleep=flush)


if __name__ == "__main__":
    unittest.main()
