"""Preflight: refusing to waste an afternoon on an unusable measurement."""

from __future__ import annotations

import os
import unittest

from oxfscope import preflight, profiles
from oxfscope.errors import OxfscopeError

from .support import fake_bpftrace, python_sleeper, requires_target, spawn_target

PROFILE = profiles.load("oncpu")
FAST = {"trial_seconds": 0.2, "attach_grace_s": 0.5}


class IndividualCheckTests(unittest.TestCase):
    def test_privileges(self):
        check = preflight.check_privileges()
        expected = preflight.PASS if os.geteuid() == 0 else preflight.FAIL
        self.assertEqual(check.status, expected)

    def test_missing_target_fails_with_a_usable_message(self):
        report = preflight.PreflightReport()
        check = preflight.check_target(report, 4_194_303)
        self.assertEqual(check.status, preflight.FAIL)
        self.assertIn("no process with pid", check.message)
        self.assertIsNotNone(check.hint)

    def test_bpftrace_version_is_parsed(self):
        with fake_bpftrace(version="0.20.2"):
            report = preflight.PreflightReport()
            check = preflight.check_bpftrace(report)
        self.assertEqual(check.status, preflight.PASS)
        self.assertEqual(report.bpftrace_version, "0.20.2")

    def test_bpftrace_too_old_is_rejected(self):
        with fake_bpftrace(version="0.11.4"):
            report = preflight.PreflightReport()
            check = preflight.check_bpftrace(report)
        self.assertEqual(check.status, preflight.FAIL)
        self.assertIn("older than", check.message)

    def test_bpftrace_missing_is_rejected(self):
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = "/nonexistent"
        try:
            check = preflight.check_bpftrace(preflight.PreflightReport())
        finally:
            os.environ["PATH"] = original
        self.assertEqual(check.status, preflight.FAIL)
        self.assertIn("not on PATH", check.message)

    def test_nofile_covers_the_estimated_need(self):
        check = preflight.check_nofile(thread_count=1, cpu_count=1)
        self.assertEqual(check.status, preflight.PASS)
        self.assertEqual(check.details["needed"], 1024)

    def test_nofile_estimate_scales_with_threads_and_cpus(self):
        check = preflight.check_nofile(thread_count=315, cpu_count=8)
        self.assertEqual(check.details["needed"], 315 * 8 * 2)

    def test_perf_event_paranoid_is_reported(self):
        check = preflight.check_perf_event_paranoid()
        self.assertIn(check.status, (preflight.PASS, preflight.WARN))
        self.assertIn("perf_event_paranoid", check.message)


class ProfileTests(unittest.TestCase):
    def test_builtin_profile_loads(self):
        self.assertEqual(profiles.load("oncpu").name, "oncpu")

    def test_reserved_profiles_say_which_milestone(self):
        for name in ("light", "standard", "deep"):
            with self.subTest(profile=name):
                with self.assertRaises(OxfscopeError) as ctx:
                    profiles.load(name)
                self.assertIn("M2", str(ctx.exception))

    def test_unknown_profile_lists_the_available_ones(self):
        with self.assertRaises(OxfscopeError) as ctx:
            profiles.load("nonsense")
        self.assertIn("oncpu", str(ctx.exception))

    def test_profile_name_is_validated_before_use(self):
        for name in ("../../etc/passwd", "a b", "Upper", ""):
            with self.subTest(name=name):
                with self.assertRaises(OxfscopeError):
                    profiles.load(name)

    def test_probe_program_must_be_a_bare_name(self):
        bad = profiles.ProbeSpec(name="x", program="../../etc/passwd")
        with self.assertRaises(OxfscopeError):
            profiles.program_path(bad)

    def test_oncpu_program_exists(self):
        self.assertTrue(profiles.program_path(profiles.ONCPU).is_file())

    def test_probe_args_are_positional_parameters(self):
        spec = profiles.ProbeSpec(name="x", program="oncpu.bt", thresholds={"min_us": 100})
        self.assertEqual(spec.probe_args(1234, 90), ("1234", "90", "100"))


@requires_target
class FullPreflightTests(unittest.TestCase):
    """The whole sequence, against a real process and a fake bpftrace."""

    def test_healthy_target_passes_every_check(self):
        with spawn_target(threads=8, seconds=30) as target, fake_bpftrace("normal"):
            report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        self.assertTrue(report.ok, [c.message for c in report.failures])
        self.assertEqual(report.thread_count, 9)
        self.assertEqual(report.comm, "contention")
        self.assertTrue(report.frame_pointers_ok)
        self.assertEqual(report.usable_probes, ["oncpu"])
        self.assertEqual(
            [c.name for c in report.checks],
            [
                "privileges",
                "bpftrace",
                "target",
                "nofile",
                "perf_event_paranoid",
                "frame_pointers",
                "smoke:oncpu",
            ],
        )

    def test_unresolved_stacks_fail_the_frame_pointer_check(self):
        """The check the whole project's credibility rests on."""
        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("empty_stacks"):
            report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        check = report.get("frame_pointers")
        self.assertEqual(check.status, preflight.FAIL)
        self.assertFalse(report.frame_pointers_ok)
        self.assertGreater(report.unknown_frame_ratio, preflight.UNKNOWN_FRAME_LIMIT)
        self.assertIn("-fno-omit-frame-pointer", check.hint)
        self.assertFalse(report.ok)

    def test_probe_that_produces_nothing_is_disabled_not_ignored(self):
        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("silent"):
            report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        self.assertFalse(report.smoke["oncpu"])
        self.assertEqual(report.usable_probes, [])
        check = report.get("smoke:oncpu")
        self.assertEqual(check.status, preflight.FAIL)  # oncpu is a required probe
        self.assertIn("printed no map data", check.message)
        self.assertIn("recorded as failed", check.message)

    def test_probe_that_cannot_attach_is_reported(self):
        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("startup_error"):
            report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        self.assertFalse(report.smoke["oncpu"])
        self.assertEqual(report.get("frame_pointers").status, preflight.FAIL)
        self.assertIn("ERROR", str(report.get("smoke:oncpu").details.get("stderr", "")))

    def test_trials_are_skipped_without_bpftrace(self):
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = "/nonexistent"
        try:
            with spawn_target(threads=4, seconds=30) as target:
                report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        finally:
            os.environ["PATH"] = original
        self.assertEqual(report.get("frame_pointers").status, preflight.SKIP)
        self.assertEqual(report.get("smoke:oncpu").status, preflight.SKIP)
        self.assertFalse(report.ok)  # the bpftrace check itself still failed

    def test_skip_trials_short_circuits_the_probe_runs(self):
        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("normal"):
            report = preflight.run_preflight(target.pid, PROFILE, skip_trials=True)
        self.assertEqual(report.get("frame_pointers").status, preflight.SKIP)
        self.assertTrue(report.ok)


class DeadTargetTests(unittest.TestCase):
    def test_nothing_downstream_runs_without_a_target(self):
        child = python_sleeper(0.01)
        child.wait()
        with fake_bpftrace("normal"):
            report = preflight.run_preflight(child.pid, PROFILE, **FAST)
        self.assertFalse(report.ok)
        self.assertEqual(report.get("target").status, preflight.FAIL)
        # Running trials against a dead pid would only produce noise.
        self.assertIsNone(report.get("frame_pointers"))
        self.assertEqual(report.smoke, {})

    def test_render_is_readable(self):
        report = preflight.PreflightReport()
        report.add(preflight.Check("a", preflight.PASS, "fine"))
        report.add(preflight.Check("b", preflight.FAIL, "broken", hint="fix it"))
        text = preflight.render(report)
        self.assertIn("[ok] a", text)
        self.assertIn("[X] b", text)
        self.assertIn("-> fix it", text)


class BpftraceEnvTests(unittest.TestCase):
    def test_map_limits_are_raised_under_both_spellings(self):
        env = preflight.bpftrace_env()
        self.assertEqual(env["BPFTRACE_MAX_MAP_KEYS"], env["BPFTRACE_MAP_KEYS_MAX"])
        self.assertGreater(int(env["BPFTRACE_MAX_MAP_KEYS"]), 4096)


if __name__ == "__main__":
    unittest.main()
