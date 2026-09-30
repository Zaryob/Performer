"""Process supervision: the part that decides whether any data survives."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from performer.runner import (
    ProbeProcess,
    SeriesSampler,
    TargetWatcher,
    scan_stderr,
    stop_all,
    wait_for_run,
)

from .support import (
    FAKE_BPFTRACE,
    python_sleeper,
    requires_target,
    spawn_target,
    wait_until,
)


def fake_argv(watchdog: float = 30.0):
    """The same positional parameters a real .bt program receives."""
    return [sys.executable, str(FAKE_BPFTRACE), "probe.bt", "1234", str(watchdog)]


class ProbeProcessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _probe(self, mode: str, watchdog: float = 30.0) -> ProbeProcess:
        return ProbeProcess(
            name="oncpu",
            argv=fake_argv(watchdog),
            stdout_path=self.tmp / "oncpu.out",
            stderr_path=self.tmp / "oncpu.err",
            env={"FAKE_BPFTRACE_MODE": mode},
        )

    def test_sigint_is_what_produces_the_data(self):
        """The behaviour the whole stop sequence exists to preserve."""
        probe = self._probe("normal")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        self.assertEqual(probe.read_stdout().strip(), "Attaching 3 probes...")
        info = probe.stop()
        self.assertEqual(info.reason, "sigint")
        self.assertEqual(info.exit_code, 0)
        self.assertIn("@cpu[", probe.read_stdout())

    def test_probe_runs_in_its_own_process_group(self):
        """So a terminal Ctrl-C reaches the collector, not the probes."""
        probe = self._probe("normal")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        self.assertNotEqual(os.getpgid(probe.pid), os.getpgid(os.getpid()))
        probe.stop()

    def test_escalates_to_sigterm_when_sigint_is_ignored(self):
        probe = self._probe("ignore_sigint")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        info = probe.stop(sigint_timeout=0.5, sigterm_timeout=2.0)
        self.assertEqual(info.reason, "sigterm")
        self.assertTrue(
            any("escalated to SIGTERM" in w for w in probe.warnings), probe.warnings
        )

    def test_a_slow_dump_is_reported_while_it_runs(self):
        """A minute of silence after Ctrl-C looks like a hang; say what is happening."""
        probe = self._probe("ignore_sigint")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        reports = []
        stop_all(
            [probe],
            sigint_timeout=0.8,
            sigterm_timeout=2.0,
            progress=lambda waiting, waited: reports.append((waiting, waited)),
            progress_every_s=0.2,
        )
        self.assertTrue(reports)
        self.assertEqual(reports[0][0], ["oncpu"])
        self.assertEqual(probe.exit_info.reason, "sigterm")
        # The shared SIGINT window is not granted twice.
        self.assertTrue(any("still running 1s after SIGINT" in w for w in probe.warnings),
                        probe.warnings)

    def test_a_quick_stop_reports_nothing(self):
        probe = self._probe("normal")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        reports = []
        stop_all([probe], progress=lambda *args: reports.append(args), progress_every_s=5.0)
        self.assertEqual(reports, [])
        self.assertEqual(probe.exit_info.reason, "sigint")

    def test_escalates_to_sigkill_and_says_the_data_is_gone(self):
        probe = self._probe("stubborn")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        info = probe.stop(sigint_timeout=0.5, sigterm_timeout=0.5)
        self.assertEqual(info.reason, "sigkill")
        self.assertTrue(
            any("never wrote its maps" in w for w in probe.warnings), probe.warnings
        )

    def test_startup_failure_is_detected_before_the_run_starts(self):
        probe = self._probe("startup_error")
        probe.start()
        self.assertFalse(probe.wait_for_attach(2.0))
        self.assertEqual(probe.exit_info.reason, "startup_error")
        self.assertIn("ERROR", probe.read_stderr())

    def test_self_exit_via_watchdog_is_reported_as_exited(self):
        probe = self._probe("normal", watchdog=0.4)
        probe.start()
        self.assertTrue(probe.wait_for_attach(0.2))
        self.assertTrue(wait_until(lambda: not probe.alive, timeout=5.0))
        info = probe.stop()
        self.assertEqual(info.reason, "exited")
        self.assertIn("@cpu[", probe.read_stdout())

    def test_missing_binary_is_a_startup_error_not_a_traceback(self):
        probe = ProbeProcess(
            name="oncpu",
            argv=["/nonexistent/bpftrace", "probe.bt"],
            stdout_path=self.tmp / "x.out",
            stderr_path=self.tmp / "x.err",
        )
        probe.start()
        self.assertFalse(probe.started)
        self.assertEqual(probe.exit_info.reason, "startup_error")
        self.assertIn("failed to execute", probe.read_stderr())

    def test_stderr_is_captured_to_its_own_file(self):
        probe = self._probe("lost_events")
        probe.start()
        self.assertTrue(probe.wait_for_attach(1.0))
        probe.stop()
        self.assertIn("Lost 12043 events", probe.read_stderr())
        self.assertTrue(self.tmp.joinpath("oncpu.err").is_file())

    def test_stop_is_idempotent(self):
        probe = self._probe("normal")
        probe.start()
        probe.wait_for_attach(1.0)
        first = probe.stop()
        second = probe.stop()
        self.assertIs(first, second)


class StderrScanTests(unittest.TestCase):
    def test_lost_events_are_summed(self):
        summary = scan_stderr("Lost 10 events\nLost 5 events\n")
        self.assertEqual(summary.events_lost, 15)
        self.assertTrue(summary.warnings)

    def test_map_full_is_reported(self):
        summary = scan_stderr("WARNING: Map full; can't update element\n")
        self.assertTrue(any("map filled up" in w for w in summary.warnings))

    def test_errors_are_flagged(self):
        self.assertTrue(scan_stderr("ERROR: no such probe\n").has_errors)
        self.assertTrue(scan_stderr("stdin:12:3-9: something\n").has_errors)

    def test_clean_stderr_is_silent(self):
        summary = scan_stderr("Attaching 2 probes...\n")
        self.assertEqual(summary.events_lost, 0)
        self.assertEqual(summary.warnings, [])
        self.assertFalse(summary.has_errors)


class TargetWatcherTests(unittest.TestCase):
    def test_notices_a_dead_target_quickly(self):
        child = python_sleeper(30)
        watcher = TargetWatcher(child.pid, interval_s=0.1)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertFalse(watcher.died.is_set())
        child.kill()
        child.wait()
        self.assertTrue(watcher.died.wait(timeout=3.0))
        self.assertIsNotNone(watcher.died_at)

    def test_live_target_is_not_reported_dead(self):
        child = python_sleeper(5)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        watcher = TargetWatcher(child.pid, interval_s=0.05)
        watcher.start()
        self.addCleanup(watcher.stop)
        time.sleep(0.3)
        self.assertFalse(watcher.died.is_set())


@requires_target
class SeriesSamplerTests(unittest.TestCase):
    def test_samples_accumulate_at_one_hertz(self):
        with spawn_target(threads=8, seconds=30, sleep_us=2000) as target:
            sampler = SeriesSampler(target.pid, interval_s=0.2)
            sampler.start()
            time.sleep(0.75)
            sampler.stop()
        self.assertGreaterEqual(len(sampler.thread_rows), 3)
        self.assertEqual(len(sampler.thread_rows), len(sampler.schedstat_rows))
        for row in sampler.thread_rows:
            self.assertEqual(len(row), 4)
            self.assertEqual(row[1], 9)  # 8 workers plus main

    def test_sampling_stops_when_the_target_dies(self):
        child = python_sleeper(30)
        sampler = SeriesSampler(child.pid, interval_s=0.05)
        sampler.start()
        time.sleep(0.15)
        child.kill()
        child.wait()
        time.sleep(0.3)
        collected = len(sampler.thread_rows)
        time.sleep(0.3)
        sampler.stop()
        self.assertEqual(len(sampler.thread_rows), collected)


class WaitForRunTests(unittest.TestCase):
    def test_duration_elapses(self):
        outcome = wait_for_run(duration_s=0.2, poll_s=0.02)
        self.assertEqual(outcome.reason, "duration")
        self.assertGreaterEqual(outcome.elapsed_s, 0.2)

    def test_target_death_wins(self):
        watcher = TargetWatcher(os.getpid(), interval_s=10)
        watcher.died.set()
        outcome = wait_for_run(duration_s=10, watcher=watcher, poll_s=0.01)
        self.assertEqual(outcome.reason, "target_died")
        self.assertLess(outcome.elapsed_s, 1.0)

    def test_operator_interrupt_wins(self):
        event = threading.Event()
        threading.Timer(0.1, event.set).start()
        outcome = wait_for_run(duration_s=10, interrupted=event, poll_s=0.01)
        self.assertEqual(outcome.reason, "interrupted")

    def test_until_exit_waits_for_the_target(self):
        watcher = TargetWatcher(os.getpid(), interval_s=10)
        threading.Timer(0.1, watcher.died.set).start()
        outcome = wait_for_run(duration_s=None, watcher=watcher, poll_s=0.01)
        self.assertEqual(outcome.reason, "target_died")


if __name__ == "__main__":
    unittest.main()
