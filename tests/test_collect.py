"""End to end collection: a live process in, a valid bundle out.

There is no bpftrace here, so the eBPF layer is a test double.  Everything
else is the real thing: a real 300-thread process, real /proc reads, real
process groups and signals, real parsing, real bundle writing and validation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from performer import layout
from performer.bundle import Bundle
from performer.collect import CollectOptions, collect
from performer.errors import PerformerError, PreflightError

from .support import fake_bpftrace, requires_target, spawn_target

#: The fake bpftrace attaches instantly, so the trial and grace windows only
#: need to be long enough to notice a process that died. Production defaults
#: are seconds; these are the same code paths, just not waited out.
FAST_PREFLIGHT = {
    "preflight_trial_s": 0.15,
    "preflight_attach_grace_s": 0.3,
    "attach_grace_s": 0.3,
}


def quiet(_message: str) -> None:
    """Collection narrates its progress; tests do not need to hear it."""


@requires_target
class CollectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _options(self, pid: int, **overrides) -> CollectOptions:
        kwargs = dict(
            pid=pid,
            label="unit",
            out_dir=self.tmp,
            duration_s=1.0,
            overhead_window_s=0.0,
            **FAST_PREFLIGHT,
        )
        kwargs.update(overrides)
        return CollectOptions(**kwargs)

    def test_produces_a_valid_bundle(self):
        with spawn_target(threads=16, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)

        self.assertIsNotNone(result.archive)
        self.assertEqual(result.manifest["status"], "ok")
        with Bundle.open(result.archive) as bundle:
            report = bundle.validate(verify_hashes=True)
            self.assertTrue(report.ok, report.flat())
            self.assertTrue(bundle.exists(layout.STACK_ONCPU))
            self.assertTrue(bundle.exists(layout.META_THREADS))
            self.assertTrue(bundle.exists(layout.META_SYSTEM))
            self.assertTrue(bundle.exists(layout.SERIES_THREADS))
            self.assertTrue(bundle.exists(f"{layout.DIR_RAW}/oncpu.stderr.log"))

    def test_explicit_bpftrace_binary_is_used_for_preflight_and_collection(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace("normal") as binary:
            result = collect(self._options(target.pid, bpftrace=binary), printer=quiet)
        self.assertEqual(result.preflight.bpftrace_path, binary)
        self.assertEqual(result.manifest["status"], "ok")
        with Bundle.open(result.archive) as bundle:
            self.assertTrue(bundle.validate().ok)

    def test_folded_stacks_are_usable(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        with Bundle.open(result.archive) as bundle:
            entries = list(bundle.iter_folded(layout.STACK_ONCPU))
        self.assertTrue(entries)
        for stack, value in entries:
            self.assertGreater(value, 0)
            self.assertNotIn(" ", stack.split(";")[-1])
        self.assertTrue(any("pthread_mutex_lock" in s for s, _v in entries))

    def test_thread_inventory_matches_the_live_process(self):
        with spawn_target(threads=64, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        with Bundle.open(result.archive) as bundle:
            threads = bundle.read_json(layout.META_THREADS)["threads"]
        self.assertEqual(len(threads), 65)  # 64 workers plus main
        self.assertEqual(result.manifest["target"]["thread_count_start"], 65)
        names = {entry["name"] for entry in threads.values()}
        self.assertIn("worker0", names)
        for entry in threads.values():
            self.assertIn("start_schedstat", entry)
            self.assertIn("end_schedstat", entry)

    def test_series_are_sampled_from_proc(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid, duration_s=2.0), printer=quiet)
        with Bundle.open(result.archive) as bundle:
            rows = bundle.series_rows(layout.SERIES_THREADS)
            sched = bundle.series_rows(layout.SERIES_SCHEDSTAT)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(len(rows), len(sched))
        self.assertEqual(int(rows[0]["thread_count"]), 9)

    def test_manifest_records_the_probe_and_its_exit(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        probe = result.manifest["probes"][0]
        self.assertEqual(probe["name"], "oncpu")
        self.assertEqual(probe["status"], "ok")
        # bpftrace only writes its maps on SIGINT, so anything else here would
        # mean the data in this bundle is not what it claims to be.
        self.assertEqual(probe["exit_reason"], "sigint")
        self.assertEqual(probe["outputs"], [layout.STACK_ONCPU])
        self.assertEqual(probe["events_lost"], 0)

    def test_raw_stdout_is_dropped_unless_requested(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            lean = collect(self._options(target.pid), printer=quiet)
            fat = collect(
                self._options(target.pid, label="kept", keep_raw_stdout=True),
                printer=quiet,
            )
        with Bundle.open(lean.archive) as bundle:
            self.assertFalse(bundle.exists(f"{layout.DIR_RAW}/oncpu.stdout.log"))
        with Bundle.open(fat.archive) as bundle:
            self.assertTrue(bundle.exists(f"{layout.DIR_RAW}/oncpu.stdout.log"))
            self.assertIn("@cpu[", bundle.read_text(f"{layout.DIR_RAW}/oncpu.stdout.log"))

    def test_preflight_report_travels_with_the_bundle(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        with Bundle.open(result.archive) as bundle:
            report = bundle.read_json(f"{layout.DIR_RAW}/preflight.json")
        self.assertTrue(report["ok"])
        self.assertEqual(report["bpftrace_version"], "0.20.2")
        self.assertEqual(report["thread_count"], 9)


@requires_target
class DegradedCollectTests(unittest.TestCase):
    """The awkward runs. These are the ones a real lab produces."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _options(self, pid: int, **overrides) -> CollectOptions:
        kwargs = dict(
            pid=pid,
            label="unit",
            out_dir=self.tmp,
            duration_s=1.0,
            overhead_window_s=0.0,
            **FAST_PREFLIGHT,
        )
        kwargs.update(overrides)
        return CollectOptions(**kwargs)

    def test_target_dying_mid_run_still_yields_a_readable_bundle(self):
        import threading
        import time

        def kill_once_collecting():
            # The run directory appears after preflight and just before the
            # probes start, so waiting for it puts the kill inside the run
            # rather than somewhere in preflight.
            while not any(self.tmp.glob("run_*")):
                time.sleep(0.02)
            time.sleep(0.3)
            target.process.kill()

        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
            threading.Thread(target=kill_once_collecting, daemon=True).start()
            result = collect(self._options(target.pid, duration_s=30.0), printer=quiet)

        self.assertEqual(result.manifest["status"], "partial")
        self.assertIsNotNone(result.manifest["target_died_at"])
        self.assertLess(result.manifest["actual_duration_s"], 30.0)
        self.assertTrue(
            any("exited before" in w for w in result.manifest["warnings"]),
            result.manifest["warnings"],
        )
        with Bundle.open(result.archive) as bundle:
            self.assertTrue(bundle.validate().ok)
            self.assertTrue(bundle.exists(layout.STACK_ONCPU))

    def test_lost_events_downgrade_the_run_to_partial(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("lost_events"):
            result = collect(self._options(target.pid), printer=quiet)
        probe = result.manifest["probes"][0]
        self.assertEqual(probe["events_lost"], 12043)
        self.assertEqual(probe["status"], "partial")
        self.assertEqual(result.manifest["status"], "partial")
        self.assertTrue(any("lower bound" in w for w in probe["warnings"]))

    def test_unresolved_stacks_block_collection(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("empty_stacks"):
            with self.assertRaises(PreflightError) as ctx:
                collect(self._options(target.pid), printer=quiet)
        self.assertIn("frame_pointers", str(ctx.exception))
        self.assertIn("--ignore-quality", str(ctx.exception))
        self.assertEqual(list(self.tmp.iterdir()), [])  # nothing half written

    def test_ignore_quality_collects_anyway_and_says_so(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("empty_stacks"):
            result = collect(
                self._options(target.pid, ignore_quality=True), printer=quiet
            )
        quality = result.manifest["quality"]
        self.assertFalse(quality["frame_pointers_ok"])
        self.assertTrue(quality["ignore_quality"])
        self.assertGreater(quality["unknown_frame_ratio"], 0.30)
        self.assertTrue(
            any("not trustworthy" in w for w in result.manifest["warnings"]),
            result.manifest["warnings"],
        )
        # And the bundle is still a valid bundle, flagged rather than absent.
        with Bundle.open(result.archive) as bundle:
            self.assertTrue(bundle.validate().ok)

    def test_probe_that_produces_nothing_stops_the_run(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("silent"):
            with self.assertRaises(PreflightError) as ctx:
                collect(self._options(target.pid), printer=quiet)
        self.assertIn("smoke:oncpu", str(ctx.exception))

    def test_all_startup_failures_show_the_probe_error(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("startup_error"):
            with self.assertRaises(PreflightError) as ctx:
                collect(self._options(target.pid), printer=quiet)
        self.assertIn("no probe survived", str(ctx.exception))
        self.assertIn("Invalid provider", str(ctx.exception))

    def test_pmu_run_survives_when_all_bpf_smoke_tests_fail(self):
        with spawn_target(threads=2, seconds=60) as target, fake_bpftrace("startup_error"), \
             patch("performer.collect.pmu_mod.Session") as session_type:
            session = session_type.return_value
            session.groups = [("cycles", "instructions")]
            session.threads = {target.pid: object()}
            session.warnings = []
            event = {"raw": 100, "scaled": 100.0, "time_enabled_ns": 1000, "time_running_ns": 1000}
            session.document.return_value = {
                "schema_version": 1, "kind": "pmu", "mode": "basic", "source": "perf_event_open",
                "scope": "user", "status": "ok", "cpu_model": "test CPU", "arch": "aarch64",
                "window_s": 1.0, "thread_count_start": 1, "threads_measured": 1,
                "events": ["cycles", "instructions"],
                "totals": {"cycles": event, "instructions": event},
                "threads": [{"tid": target.pid, "name": "target", "start_time_ticks": 1,
                             "coverage_s": 1.0, "events": {"cycles": event, "instructions": event}}],
                "warnings": [],
            }
            result = collect(self._options(target.pid, pmu="basic"), printer=quiet)
        self.assertEqual(result.manifest["status"], "partial")
        with Bundle.open(result.archive) as bundle:
            self.assertTrue(bundle.validate().ok)
            self.assertTrue(bundle.exists(layout.PMU_COUNTERS))

    def test_failed_oncpu_is_warning_when_other_probes_work(self):
        with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("oncpu_startup_error"):
            result = collect(
                self._options(target.pid, profile_name="standard"), printer=quiet
            )
        self.assertEqual(result.manifest["status"], "partial")
        self.assertEqual(result.preflight.get("frame_pointers").status, "warn")
        self.assertEqual(result.preflight.get("smoke:oncpu").status, "warn")
        self.assertFalse(result.preflight.smoke["oncpu"])
        self.assertTrue(result.preflight.smoke["futex"])
        with Bundle.open(result.archive) as bundle:
            self.assertTrue(bundle.validate().ok)
            self.assertFalse(bundle.exists(layout.STACK_ONCPU))
            self.assertTrue(bundle.exists(layout.HIST_FUTEX_BY_ADDR))

    def test_missing_bpftrace_is_refused_before_anything_is_written(self):
        import os

        with spawn_target(threads=4, seconds=30) as target:
            original = os.environ.get("PATH", "")
            os.environ["PATH"] = "/nonexistent"
            try:
                with self.assertRaises(PreflightError) as ctx:
                    collect(self._options(target.pid), printer=quiet)
            finally:
                os.environ["PATH"] = original
        self.assertIn("bpftrace", str(ctx.exception))
        self.assertEqual(list(self.tmp.iterdir()), [])


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_duration_beyond_the_profile_limit_is_refused(self):
        options = CollectOptions(
            pid=1, label="unit", out_dir=self.tmp, duration_s=6000.0
        )
        with self.assertRaises(PerformerError) as ctx:
            collect(options, printer=quiet)
        self.assertIn("--force", str(ctx.exception))

    def test_zero_duration_is_refused(self):
        options = CollectOptions(pid=1, label="unit", out_dir=self.tmp, duration_s=0)
        with self.assertRaises(PerformerError):
            collect(options, printer=quiet)

    def test_missing_bpftrace_stops_before_target_checks(self):
        options = CollectOptions(
            pid=4194303, label="unit", out_dir=self.tmp,
            bpftrace="/nonexistent/performer-bpftrace",
        )
        with self.assertRaises(PreflightError) as ctx:
            collect(options, printer=quiet)
        self.assertIn("bpftrace", str(ctx.exception))
        self.assertNotIn("target", str(ctx.exception))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_unknown_profile_is_refused_before_touching_the_target(self):
        options = CollectOptions(
            pid=1, label="unit", out_dir=self.tmp, profile_name="nosuchprofile"
        )
        with self.assertRaises(PerformerError) as ctx:
            collect(options, printer=quiet)
        self.assertIn("standard", str(ctx.exception))

    def test_profile_name_cannot_escape_the_profiles_directory(self):
        options = CollectOptions(
            pid=1, label="unit", out_dir=self.tmp, profile_name="../../etc/passwd"
        )
        with self.assertRaises(PerformerError):
            collect(options, printer=quiet)


@requires_target
class StandardProfileTests(unittest.TestCase):
    """M2's acceptance criteria, on the full probe set."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _options(self, pid: int, **overrides) -> CollectOptions:
        kwargs = dict(
            pid=pid,
            label="unit",
            out_dir=self.tmp,
            profile_name="standard",
            duration_s=2.0,
            overhead_window_s=0.0,
            **FAST_PREFLIGHT,
        )
        kwargs.update(overrides)
        return CollectOptions(**kwargs)

    def test_every_probe_in_the_profile_delivers(self):
        with spawn_target(threads=32, seconds=90) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)

        statuses = {p["name"]: p["status"] for p in result.manifest["probes"]}
        self.assertEqual(
            statuses,
            {
                "oncpu": "ok",
                "runqlat": "ok",
                "threadlife": "ok",
                "offcpu": "ok",
                "futex": "ok",
                "wakeup": "ok",
            },
        )
        self.assertEqual(result.manifest["status"], "ok")

    def test_the_bundle_holds_every_expected_artifact(self):
        with spawn_target(threads=32, seconds=90) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        expected = {
            layout.STACK_ONCPU,
            layout.STACK_OFFCPU,
            layout.STACK_FUTEX,
            layout.HIST_RUNQLAT,
            layout.HIST_OFFCPU_DURATION,
            layout.HIST_OFFCPU_BY_STATE,
            layout.HIST_FUTEX_BY_ADDR,
            layout.HIST_FUTEX_DURATION,
            layout.HIST_THREADLIFE,
            layout.HIST_THREAD_LIFETIME,
            layout.GRAPH_WAKEUP_EDGES,
            layout.META_SYSTEM,
            layout.META_TARGET,
            layout.META_THREADS,
            layout.SERIES_THREADS,
            layout.SERIES_SCHEDSTAT,
        }
        with Bundle.open(result.archive) as bundle:
            present = set(bundle.paths())
            self.assertTrue(expected <= present, sorted(expected - present))
            self.assertTrue(bundle.validate(verify_hashes=True).ok)

    def test_thresholds_are_recorded_because_they_change_the_meaning(self):
        with spawn_target(threads=8, seconds=90) as target, fake_bpftrace("normal"):
            result = collect(self._options(target.pid), printer=quiet)
        probes = {p["name"]: p for p in result.manifest["probes"]}
        self.assertEqual(probes["offcpu"]["thresholds"], {"min_us": 100.0})
        self.assertEqual(probes["futex"]["thresholds"], {"min_us": 50.0})
        self.assertNotIn("thresholds", probes["oncpu"])

    def test_overhead_stays_under_the_profile_budget(self):
        """M2 acceptance: the standard profile costs the target under 15%.

        With no real eBPF in this environment this measures the collector's
        own cost -- probe supervision plus 1 Hz /proc sampling of every thread
        -- which is the part this project controls.
        """
        with spawn_target(threads=315, seconds=120) as target, fake_bpftrace("normal"):
            result = collect(
                self._options(target.pid, duration_s=8.0, overhead_window_s=3.0),
                printer=quiet,
            )
        overhead = result.manifest["quality"]["estimated_overhead_pct"]
        self.assertLess(overhead, 15.0, f"estimated overhead {overhead}%")

    def test_overhead_is_not_inflated_by_probe_startup(self):
        """The CPU window and the clock must open at the same instant.

        Reading the counter before the probes attach would charge the attach
        time to a window measured from after it, and the error would grow with
        the number of probes -- exaggerating exactly the runs that trace most.
        """
        with spawn_target(threads=16, seconds=90) as target, fake_bpftrace("normal"):
            one = collect(
                CollectOptions(
                    pid=target.pid, label="one", out_dir=self.tmp,
                    profile_name="light", duration_s=3.0, overhead_window_s=1.0,
                    **FAST_PREFLIGHT,
                ),
                printer=quiet,
            )
            many = collect(
                self._options(target.pid, label="many", duration_s=3.0,
                              overhead_window_s=1.0),
                printer=quiet,
            )
        light_probes = len(one.manifest["probes"])
        standard_probes = len(many.manifest["probes"])
        self.assertGreater(standard_probes, light_probes)
        for result in (one, many):
            overhead = result.manifest["quality"]["estimated_overhead_pct"]
            self.assertLess(overhead, 25.0, f"{result.manifest['label']}: {overhead}%")

    def test_target_killed_mid_run_leaves_a_partial_but_readable_bundle(self):
        """M2 acceptance: the awkward case a real lab produces."""
        import threading
        import time

        def kill_once_every_probe_is_running():
            # wakeup is the last probe the standard profile launches; once it
            # has printed its attach line every probe is up and the run is
            # genuinely underway.
            marker = "raw/wakeup.stdout.log"
            while True:
                found = list(self.tmp.glob(f"run_*/{marker}"))
                if found and found[0].stat().st_size > 0:
                    break
                time.sleep(0.02)
            time.sleep(0.5)
            target.process.kill()

        with spawn_target(threads=32, seconds=120) as target, fake_bpftrace("normal"):
            threading.Thread(
                target=kill_once_every_probe_is_running, daemon=True
            ).start()
            result = collect(
                self._options(target.pid, duration_s=30.0), printer=quiet
            )

        self.assertEqual(result.manifest["status"], "partial")
        self.assertIsNotNone(result.manifest["target_died_at"])
        self.assertLess(result.manifest["actual_duration_s"], 30.0)
        with Bundle.open(result.archive) as bundle:
            report = bundle.validate(verify_hashes=True)
            self.assertTrue(report.ok, report.flat())
            # Everything collected before the target died is still there.
            self.assertTrue(bundle.exists(layout.STACK_OFFCPU))
            self.assertTrue(bundle.exists(layout.HIST_FUTEX_BY_ADDR))
            self.assertTrue(bundle.exists(layout.GRAPH_WAKEUP_EDGES))

    def test_deep_profile_refuses_a_long_run_without_force(self):
        options = CollectOptions(
            pid=1, label="unit", out_dir=self.tmp,
            profile_name="deep", duration_s=120.0,
        )
        with self.assertRaises(PerformerError) as ctx:
            collect(options, printer=quiet)
        self.assertIn("--force", str(ctx.exception))
        self.assertIn("60", str(ctx.exception))


@requires_target
class CliCollectTests(unittest.TestCase):
    """The command line path, including its exit code."""

    def test_collect_via_cli(self):
        from .test_cli import run_cli

        with tempfile.TemporaryDirectory() as tmp:
            with spawn_target(threads=8, seconds=60) as target, fake_bpftrace("normal"):
                code, out, err = run_cli(
                    "collect",
                    "--pid", str(target.pid),
                    "--duration", "1",
                    "--label", "clitest",
                    "--tag", "smoke",
                    "--out", tmp,
                    "--overhead-window", "0",
                )
            self.assertEqual(code, 0, err)
            self.assertIn("status:        ok", out)
            archive = sorted(Path(tmp).glob("*.tgz"))[0]

            code, out, _ = run_cli("inspect", "--json", str(archive))
            self.assertEqual(code, 0)
            document = json.loads(out)
            self.assertTrue(document["schema_valid"])
            self.assertEqual(document["flags"], [])
            self.assertEqual(document["target"]["comm"], "contention")

    def test_preflight_command(self):
        from .test_cli import run_cli

        with spawn_target(threads=4, seconds=30) as target, fake_bpftrace("normal"):
            code, out, err = run_cli(
                "preflight", "--pid", str(target.pid), "--skip-trials"
            )
        self.assertEqual(code, 0, err)
        self.assertIn("[ok] privileges", out)
        self.assertIn("[-] frame_pointers", out)

    def test_preflight_exit_code_reports_failure(self):
        from .test_cli import run_cli

        code, _out, _err = run_cli("preflight", "--pid", "4194303", "--skip-trials")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()


class InterruptGuardTests(unittest.TestCase):
    def test_a_second_ctrl_c_does_not_kill_the_collector(self):
        """bpftrace may still be writing its maps; a KeyboardInterrupt would lose them."""
        import os
        import signal
        import threading
        import time

        from performer.collect import _interrupt_guard

        event = threading.Event()
        messages = []
        with _interrupt_guard(event, messages.append):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
        self.assertTrue(event.is_set())
        self.assertEqual(len(messages), 2)
        self.assertIn("interrupted", messages[0])
        self.assertIn("already stopping", messages[1])
