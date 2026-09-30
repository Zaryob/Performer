"""Preflight: refusing to waste an afternoon on an unusable measurement."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from performer import preflight, profiles

from .support import fake_bpftrace, python_sleeper, requires_target, spawn_target

#: A one probe profile: these tests are about the checks, not the probe set,
#: and every extra probe costs another trial run.
PROFILE = profiles.Profile(
    name="unit",
    description="single probe, for preflight tests",
    probes=(profiles.ONCPU,),
    max_duration_s=600,
    expected_overhead="< 3%",
)
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
        self.assertIn("missing or not executable", check.message)

    def test_requested_bpftrace_binary_is_checked(self):
        with fake_bpftrace(version="0.20.2") as binary:
            report = preflight.PreflightReport()
            check = preflight.check_bpftrace(report, binary=binary)
        self.assertEqual(check.status, preflight.PASS)
        self.assertEqual(report.bpftrace_path, binary)

    def test_missing_tools_stop_before_target_and_trials(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"PATH": "/nonexistent", "PERFORMER_PROBES_DIR": directory}
        ), mock.patch.object(preflight, "check_target") as target, mock.patch.object(
            preflight, "run_trial"
        ) as trial:
            report = preflight.run_preflight(999999, PROFILE)
        self.assertEqual(report.get("bpftrace").status, preflight.FAIL)
        self.assertEqual(report.get("probe_programs").status, preflight.FAIL)
        self.assertEqual(report.get("frame_pointers").status, preflight.SKIP)
        rendered = preflight.render(report)
        self.assertIn("oncpu.bt", rendered)
        self.assertIn("probe trials", rendered)
        self.assertNotIn("smoke:oncpu", rendered)
        target.assert_not_called()
        trial.assert_not_called()

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
                "bpftrace",
                "probe_programs",
                "privileges",
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
        self.assertFalse(report.ok)
        check = report.get("smoke:oncpu")
        self.assertEqual(check.status, preflight.WARN)
        self.assertIn("printed no map data", check.message)
        self.assertIn("recorded as failed", check.message)

    def test_probe_that_cannot_attach_is_reported(self):
        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("startup_error"):
            report = preflight.run_preflight(target.pid, PROFILE, **FAST)
        self.assertFalse(report.smoke["oncpu"])
        self.assertEqual(report.get("frame_pointers").status, preflight.WARN)
        self.assertIn("ERROR", str(report.get("smoke:oncpu").details.get("stderr", "")))
        self.assertIn("Invalid provider", preflight.render(report))

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

    def test_a_repeated_cause_is_named_under_every_probe(self):
        """Printed once, the cause made only the first probe look broken."""
        stderr = "ERROR: Could not resolve symbol: /proc/self/exe:END_trigger\n"
        report = preflight.PreflightReport()
        for name in ("smoke:runqlat", "smoke:futex"):
            report.add(
                preflight.Check(
                    name, preflight.WARN, "no data", details={"stderr": stderr}
                )
            )
        text = preflight.render(report)
        self.assertEqual(text.count("END_trigger"), 1)
        self.assertIn("cause: same as smoke:runqlat", text)


class SmokeReuseTests(unittest.TestCase):
    def test_a_trial_that_already_ran_the_probe_is_reused(self):
        """The frame pointer trial is an oncpu run; a second one can miss an idle target."""
        trial = preflight.TrialResult(
            ran=True, stdout="", stderr="", exit_reason="sigint", map_entries=4
        )
        report = preflight.PreflightReport()
        with mock.patch.object(preflight, "run_trial") as run:
            checks = preflight.check_smoke(report, PROFILE, 1, reuse={"oncpu": trial})
        run.assert_not_called()
        self.assertTrue(report.smoke["oncpu"])
        self.assertEqual([c.status for c in checks], [preflight.PASS])

    def test_probes_without_a_reusable_trial_still_run(self):
        silent = preflight.TrialResult(
            ran=True, stdout="", stderr="", exit_reason="sigint"
        )
        report = preflight.PreflightReport()
        with mock.patch.object(preflight, "run_trial", return_value=silent) as run:
            checks = preflight.check_smoke(report, PROFILE, 1, reuse={})
        run.assert_called_once()
        self.assertFalse(report.smoke["oncpu"])
        self.assertEqual([c.status for c in checks], [preflight.WARN])


class BpftraceEnvTests(unittest.TestCase):
    def test_map_limits_are_raised_under_both_spellings(self):
        env = preflight.bpftrace_env()
        self.assertEqual(env["BPFTRACE_MAX_MAP_KEYS"], env["BPFTRACE_MAP_KEYS_MAX"])
        self.assertGreater(int(env["BPFTRACE_MAX_MAP_KEYS"]), 4096)


if __name__ == "__main__":
    unittest.main()
