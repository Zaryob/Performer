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

from performer import layout
from performer.bundle import Bundle
from performer.collect import CollectOptions, collect
from performer.errors import PerformerError, PreflightError

from .support import fake_bpftrace, requires_target, spawn_target

FAST_PREFLIGHT = {"preflight_trial_s": 0.2, "preflight_attach_grace_s": 0.5}


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

    def test_unknown_profile_is_refused_before_touching_the_target(self):
        options = CollectOptions(
            pid=1, label="unit", out_dir=self.tmp, profile_name="standard"
        )
        with self.assertRaises(PerformerError) as ctx:
            collect(options, printer=quiet)
        self.assertIn("M2", str(ctx.exception))


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
