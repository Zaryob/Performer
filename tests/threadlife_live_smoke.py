#!/usr/bin/env python3
"""Optional Linux/eBPF check: observe 20 transient threads and exclude a child process.

Run as root with bpftrace installed. --include is only for environments where
old bpftrace needs explicit tracepoint typedefs (e.g. a newer Docker VM kernel).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
from performer import profiles
from performer.parse.events import thread_events
from performer.preflight import bpftrace_env
from performer.runner import ProbeProcess


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bpftrace", default="bpftrace")
    parser.add_argument("--include", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("requires Linux, root and eBPF access")
    with tempfile.TemporaryDirectory(prefix="performer-threadlife-") as tmp:
        directory = Path(tmp)
        go, done = directory / "go", directory / "done"
        source = '''import pathlib, subprocess, sys, threading, time
while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)
subprocess.run([sys.executable, '-c', 'pass'], check=True)
for _ in range(20):
    t = threading.Thread(target=lambda: time.sleep(0.002))
    t.start()
    t.join()
pathlib.Path(sys.argv[2]).touch()
time.sleep(90)
'''
        target = subprocess.Popen([sys.executable, "-c", source, str(go), str(done)])
        spec = profiles.load("light").probe("threadlife")
        command = profiles.probe_command(spec, target.pid, 90, bpftrace=args.bpftrace)
        if args.include is not None:
            command[1:1] = ["--include", str(args.include)]
        probe = ProbeProcess("threadlife", command, directory / "stdout", directory / "stderr", env=bpftrace_env(spec))
        try:
            probe.start()
            if not probe.wait_for_attach(60):
                raise RuntimeError("threadlife did not become ready: " + probe.read_stderr())
            go.touch()
            deadline = time.monotonic() + 10
            while not done.exists():
                if time.monotonic() > deadline or target.poll() is not None:
                    raise RuntimeError("thread churn target did not finish")
                time.sleep(0.02)
            probe.stop()
            document, warnings, partial = thread_events(probe.read_stdout())
            created = [row for row in document["rows"] if row[2] == "created"]
            exited = [row for row in document["rows"] if row[2] == "exited"]
            if len(created) != 20 or len(exited) != 20 or partial:
                raise RuntimeError(f"expected 20 created/exited threads, got {len(created)}/{len(exited)}; {warnings}; {probe.read_stderr()}")
            if not all(row[4] == target.pid for row in created) or not all(row[7] is not None for row in exited):
                raise RuntimeError("missing parent or lifetime evidence")
            print(json.dumps({"status": "passed", "created": 20, "exited": 20, "child_process_excluded": True}))
        finally:
            probe.stop()
            if target.poll() is None:
                target.kill()
            target.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
