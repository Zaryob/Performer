"""Real local control IPC and prepared collector lifecycle, including failures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import time
import unittest

from performer import layout
from performer.bundle import Bundle
from performer.collect import CollectOptions, collect
from performer.control import CaptureControl, request
from performer.errors import PerformerError
from .support import fake_bpftrace, requires_target, spawn_target


class ControlSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "c.sock"
        self.controller = CaptureControl(self.path, os.getpid())
        self.controller.open()
        self.stop = threading.Event()
        self.errors = []
        def serve():
            try:
                while not self.stop.is_set():
                    received = self.controller.receive()
                    if received:
                        connection, command = received
                        self.controller.state = "recording" if command == "start" else "finalizing"
                        self.controller.reply(connection)
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.stop.set()
        self.thread.join(3)
        self.controller.close()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_permissions_pid_checks_and_state_transitions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(request(self.path, "status", pid=os.getpid())["state"], "ready")
        with self.assertRaisesRegex(PerformerError, "PID does not match"):
            request(self.path, "start", pid=os.getpid() + 1)
        with self.assertRaisesRegex(PerformerError, "cannot 'stop'"):
            request(self.path, "stop")
        self.assertEqual(request(self.path, "start")["state"], "recording")
        with self.assertRaisesRegex(PerformerError, "cannot 'start'"):
            request(self.path, "start")
        self.assertEqual(request(self.path, "stop")["state"], "finalizing")
        self.assertIn("start_client_ns", self.controller.metadata)
        self.assertIn("stop_request_ns", self.controller.metadata)

    def test_invalid_messages_do_not_crash_or_start_the_capture(self):
        for payload in (b'[]\n', b'not-json\n', b'{"command":[]}\n', b'{"command":"start","source":"code"}\n', b'x' * 4097 + b'\n'):
            with self.subTest(payload=payload[:40]), socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(3)
                connection.connect(str(self.path))
                connection.sendall(payload)
                response = json.loads(connection.recv(4096))
                self.assertFalse(response["ok"])
        self.assertEqual(request(self.path, "status")["state"], "ready")

    def test_existing_path_is_never_replaced(self):
        second = CaptureControl(self.path, os.getpid())
        with self.assertRaisesRegex(PerformerError, "cannot create"):
            second.open()
        second.close()
        self.assertEqual(request(self.path, "status")["state"], "ready")

    def test_cleanup_preserves_a_replacement_file(self):
        self.stop.set()
        self.thread.join(3)
        self.path.unlink()
        self.path.write_text("unrelated replacement")
        self.controller.close()
        self.assertEqual(self.path.read_text(), "unrelated replacement")


@requires_target
@unittest.skipUnless(os.path.isdir('/proc/self'), 'requires Linux /proc')
class ControlledCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "c.sock"

    def launch(self, pid, **extra):
        options = CollectOptions(pid=pid, label="controlled", out_dir=self.root / "runs",
                                 profile_name="light", duration_s=extra.pop("duration_s", 2),
                                 control_socket=self.path, control_timeout_s=extra.pop("control_timeout_s", 3),
                                 attach_grace_s=1, **extra)
        result, errors = [], []
        cancel = threading.Event()
        def run():
            try:
                result.append(collect(options, printer=lambda _message: None, cancel=cancel))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        def cleanup():
            cancel.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not self.path.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        return thread, result, errors, cancel

    def test_stopped_target_prepares_without_trials_and_records_only_after_start(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace():
            os.kill(target.pid, signal.SIGSTOP)
            thread, results, errors, _cancel = self.launch(target.pid)
            self.assertTrue(self.path.exists(), errors)
            self.assertEqual(request(self.path, "status")["state"], "ready")
            time.sleep(0.1)
            self.assertEqual(list((self.root / "runs").rglob(".window-control.json")), [])
            started = request(self.path, "start", pid=target.pid)
            os.kill(target.pid, signal.SIGCONT)
            time.sleep(0.1)
            os.kill(target.pid, signal.SIGSTOP)
            stopped = request(self.path, "stop", pid=target.pid)
            thread.join(5)
            self.assertEqual(errors, [])
            self.assertFalse(thread.is_alive())
            self.assertFalse(self.path.exists())
            self.assertEqual(results[0].manifest["status"], "ok")
            with Bundle.open(results[0].archive) as bundle:
                self.assertTrue(bundle.validate(verify_hashes=True).ok)
                timing = bundle.read_json(layout.META_WINDOW)
                self.assertTrue(timing["certified"])
                self.assertEqual(timing["start_ns"], int(started["start_ns"]))
                self.assertEqual(timing["end_ns"], int(stopped["end_ns"]))
                self.assertEqual(timing["control"]["ended_by"], "controlled_stop")
                self.assertTrue(0.1 < (timing["end_ns"] - timing["start_ns"]) / 1e9 < 1)
                self.assertTrue(all(check["status"] == "skip" for check in bundle.read_json("raw/preflight.json")["checks"] if check["name"].startswith("smoke:")))
                self.assertIsNone(bundle.manifest["quality"]["estimated_overhead_pct"])

    def test_safety_duration_without_stop_marks_capture_partial(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace():
            thread, results, errors, _cancel = self.launch(target.pid, duration_s=0.1)
            request(self.path, "start")
            thread.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(results[0].manifest["status"], "partial")
            self.assertTrue(any("before a stop command" in warning for warning in results[0].manifest["warnings"]))

    def test_cancel_before_start_closes_control_and_writes_no_measurement(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace():
            thread, results, errors, cancel = self.launch(target.pid)
            cancel.set()
            thread.join(5)
            self.assertEqual(results, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("cancelled before start", str(errors[0]))
            self.assertFalse(self.path.exists())
            self.assertEqual(list((self.root / "runs").rglob("manifest.json")), [])

    def test_prepared_capture_times_out_and_cleans_up(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace():
            thread, results, errors, _cancel = self.launch(target.pid, control_timeout_s=0.1)
            thread.join(5)
            self.assertEqual(results, [])
            self.assertIn("timed out waiting", str(errors[0]))
            self.assertFalse(self.path.exists())

    def test_target_exit_before_start_cleans_up(self):
        with spawn_target(threads=4, seconds=60) as target, fake_bpftrace():
            thread, results, errors, _cancel = self.launch(target.pid)
            target.kill()
            thread.join(5)
            self.assertEqual(results, [])
            self.assertIn("target exited before", str(errors[0]))
            self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
