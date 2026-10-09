#!/usr/bin/env python3
"""Capture a real perf/Performer pair in a privileged host-PID Linux container.

perf surrounds preflight and capture; the comparison tool clips its samples
to Performer's certified kernel timestamp window. Simultaneous profiling can
affect the workload, so this validates coverage rather than isolated overhead.
"""
import argparse
import datetime
import json
import pathlib
import shutil
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "collector"))
from performer.collect import CollectOptions, collect


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--perf", default=shutil.which("perf"))
    parser.add_argument("--target", type=pathlib.Path, default=ROOT / "tests/target/contention")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--hz", type=int, default=99)
    parser.add_argument("--threads", type=int, default=120)
    parser.add_argument("--ignore-quality", action="store_true", help="retain an explicitly flagged comparison when unwinding/symbols are incomplete")
    args = parser.parse_args()
    if not args.perf or not shutil.which("bpftrace"):
        parser.error("perf and bpftrace must both be installed")
    subprocess.run([args.perf, "--version"], check=True)
    if not args.target.is_file():
        parser.error("build the Linux target first: make target")
    args.out.mkdir(parents=True, exist_ok=True)
    target = subprocess.Popen([
        str(args.target), "--threads", str(args.threads), "--seconds", "0",
        "--contention", "50", "--hold-us", "20", "--sleep-us", "200",
    ], stdout=subprocess.PIPE, text=True)
    recorder = None
    record_log = (args.out / "perf-record.log").open("w")
    started = utc_now()
    try:
        while target.stdout.readline().strip() != "ready":
            if target.poll() is not None:
                raise RuntimeError("target exited before readiness")
        print(f"target PID {target.pid}, {args.threads} workers", flush=True)
        recorder = subprocess.Popen([
            args.perf, "record", "--clockid", "CLOCK_BOOTTIME", "-e", "cpu-clock",
            "-F", str(args.hz), "--call-graph", "fp", "-p", str(target.pid),
            "-o", str(args.out / "perf.data"),
        ], stderr=record_log)
        time.sleep(0.3)
        if recorder.poll() is not None:
            raise RuntimeError("perf record failed; see perf-record.log")
        result = collect(CollectOptions(
            pid=target.pid, label="common-window-120", out_dir=args.out,
            duration_s=args.duration, profile_name="standard", oncpu_hz=args.hz,
            preflight_trial_s=1, overhead_window_s=2, keep_raw_stdout=True,
            ignore_quality=args.ignore_quality,
        ))
        print(f"bundle {result.archive}", flush=True)
    finally:
        if recorder is not None and recorder.poll() is None:
            recorder.send_signal(signal.SIGINT)
            recorder.wait(timeout=30)
        record_log.close()
        (args.out / "perf-meta.json").write_text(json.dumps({
            "pid": target.pid, "event": "cpu-clock", "sample_hz": args.hz,
            "call_graph": "fp", "clock": "boottime",
            "started_at": started, "ended_at": utc_now(),
            "simultaneous_recording": True,
            "overhead_note": "two profilers were active; this is not an isolated overhead measurement",
        }, indent=2))
        if target.poll() is None:
            target.terminate()
        target.wait(timeout=10)
        if target.stdout is not None:
            target.stdout.close()
    # perf flushes a valid recording on SIGINT; some builds preserve the
    # signal exit code. The subsequent perf script invocation validates it.
    if recorder.returncode not in (0, -signal.SIGINT, 128 + signal.SIGINT):
        raise RuntimeError("perf record did not finish successfully")
    with (args.out / "perf-script.txt").open("w") as stream:
        subprocess.run([
            args.perf, "script", "--ns", "-i", str(args.out / "perf.data"),
            "-F", "comm,pid,tid,time,event,ip,sym,dso",
        ], stdout=stream, check=True)
    subprocess.run([
        sys.executable, str(ROOT / "tests/perf_compare.py"), str(result.archive),
        str(args.out / "perf-script.txt"), "--perf-meta", str(args.out / "perf-meta.json"),
        "--json", str(args.out / "comparison.json"),
    ], check=True)


if __name__ == "__main__":
    main()
