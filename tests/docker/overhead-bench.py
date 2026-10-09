#!/usr/bin/env python3
"""Measure what profiling costs the contention target, run by run.

Each run starts a fresh target with ``--report-ms`` so the target itself
reports iterations and process CPU on CLOCK_BOOTTIME. Modes alternate with a
rotating order every round, so slow drift (thermal, frequency governor,
background load) does not land on one mode:

* ``baseline``  -- untraced, a window of ``--duration`` after ``--settle``
* ``performer`` -- the standard profile; the window is its certified gate
* ``perf``      -- ``perf record -e cpu-clock -F HZ --call-graph fp``

Workloads use the same binary with different knobs: ``cpu`` (pure user
CPU), ``lock`` (one hot mutex) and ``io`` (write + fdatasync per iteration on
``--io-dir``, which must be a real block device, not tmpfs).

Concurrent profilers are never combined here; this complements
``perf-compare.py``, which measures coverage, not cost. Summarise with
``python3 tests/overhead_bench.py OUT/results.json``.
"""
import argparse
import datetime
import json
import os
import pathlib
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT))
from performer import layout
from performer.bundle import Bundle
from performer.collect import CollectOptions, collect
from performer.parse.perf_data import recorded_loss
from performer.provenance import tool_versions
from performer.window import clock_now_ns
from tests import overhead_bench

WORKLOADS = {
    "cpu": ["--contention", "0", "--sleep-us", "0", "--work", "200"],
    "lock": ["--contention", "90", "--hold-us", "50", "--sleep-us", "0", "--work", "50"],
    "io": ["--contention", "0", "--sleep-us", "0", "--work", "10", "--io-kb", "16"],
}
MODES = ("baseline", "performer", "perf")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _command(*args):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return (done.stdout or done.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"


def _read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None


def environment(args):
    cpu_model = None
    for line in (_read("/proc/cpuinfo") or "").splitlines():
        if line.lower().startswith(("model name", "hardware")):
            cpu_model = line.split(":", 1)[1].strip()
            break
    if cpu_model is None:
        cpu_model = _command("lscpu")
        cpu_model = next((l.split(":", 1)[1].strip() for l in cpu_model.splitlines()
                          if l.startswith("Model name")), None)
    return {
        "recorded_at": utc_now(),
        "kernel": platform.release(),
        "os_release": _read("/etc/os-release"),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "online_cpus": os.cpu_count(),
        "cpu_governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "memory": next((l for l in (_read("/proc/meminfo") or "").splitlines() if l.startswith("MemTotal")), None),
        "python": platform.python_version(),
        "bpftrace": _command("bpftrace", "--version"),
        "perf": _command(args.perf, "--version") if args.perf else None,
        "euid": os.geteuid(),
        "perf_event_paranoid": _read("/proc/sys/kernel/perf_event_paranoid"),
        "kptr_restrict": _read("/proc/sys/kernel/kptr_restrict"),
        "io_dir": str(args.io_dir),
        "io_dir_filesystem": _command("findmnt", "-no", "FSTYPE,SOURCE", "--target", str(args.io_dir)),
        "tool_versions": tool_versions(),
        "settings": {
            "duration_s": args.duration, "settle_s": args.settle, "rounds": args.rounds,
            "threads": args.threads, "sample_hz": args.hz, "profile": args.profile,
            "workloads": {name: WORKLOADS[name] for name in args.workloads},
            "modes": list(args.modes), "report_ms": args.report_ms,
            "perf_event": "cpu-clock", "perf_call_graph": "fp",
        },
    }


class Target:
    """The workload process plus a thread that keeps every tick line."""

    def __init__(self, binary, workload, threads, report_ms, io_dir):
        argv = [str(binary), "--threads", str(threads), "--seconds", "0",
                "--report-ms", str(report_ms), "--io-dir", str(io_dir), *WORKLOADS[workload]]
        self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        self.lines = []
        self._ready = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if not self._ready.wait(15):
            self.stop()
            raise RuntimeError("target did not become ready")

    def _read(self):
        for line in self.process.stdout:
            line = line.strip()
            if line == "ready":
                self._ready.set()
            self.lines.append(line)

    @property
    def pid(self):
        return self.process.pid

    def ticks(self):
        return overhead_bench.parse_ticks(list(self.lines))

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._reader.join(timeout=5)


def _wait_ticks_past(target, end_ns, report_ms):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ticks = target.ticks()
        if ticks and ticks[-1][0] >= end_ns:
            return
        time.sleep(report_ms / 1000)


def run_baseline(target, args, run):
    time.sleep(args.settle)
    start = clock_now_ns()
    time.sleep(args.duration)
    end = clock_now_ns()
    _wait_ticks_past(target, end, args.report_ms)
    run["window"] = {"start_ns": start, "end_ns": end, "source": "harness clock"}


def run_performer(target, args, run, out):
    started = time.monotonic()
    result = collect(CollectOptions(
        pid=target.pid, label=f"{run['workload']}-r{run['round']}", out_dir=out / "bundles",
        duration_s=args.duration, profile_name=args.profile, oncpu_hz=args.hz,
        preflight_trial_s=1, overhead_window_s=2,
    ), printer=lambda _m: None)
    wall = time.monotonic() - started
    manifest = result.manifest
    with Bundle.open(result.archive) as bundle:
        window = bundle.read_json(layout.META_WINDOW)
    _wait_ticks_past(target, window["end_ns"], args.report_ms)
    losses = [p.get("events_lost") for p in manifest["probes"]]
    run.update({
        "bundle": str(pathlib.Path(result.archive).relative_to(out)),
        "window": {"start_ns": window["start_ns"], "end_ns": window["end_ns"],
                   "source": "meta/window.json", "certified": window.get("certified")},
        "status": manifest["status"],
        "probes": {p["name"]: p["status"] for p in manifest["probes"]},
        "events_lost": None if any(v is None for v in losses) else sum(losses),
        "self_estimated_overhead_pct": manifest["quality"].get("estimated_overhead_pct"),
        "quality_notes": manifest["quality"].get("notes", []),
        "collector_wall_s": wall,
        "outside_window_s": wall - (window["end_ns"] - window["start_ns"]) / 1e9,
    })
    failed = [name for name, status in run["probes"].items() if status != "ok"]
    if failed or not window.get("certified"):
        run["error"] = "probes not ok: " + ", ".join(failed) if failed else "window not certified"


def run_perf(target, args, run, out):
    data = out / "perf" / f"{run['workload']}-r{run['round']}.data"
    data.parent.mkdir(parents=True, exist_ok=True)
    log = data.with_suffix(".log")
    seconds = args.settle + args.duration + 0.5
    launched = clock_now_ns()
    with log.open("w") as stream:
        recorder = subprocess.run([
            args.perf, "record", "-e", "cpu-clock", "-F", str(args.hz), "--call-graph", "fp",
            "-p", str(target.pid), "-o", str(data), "--", "sleep", f"{seconds:.3f}",
        ], stdout=stream, stderr=subprocess.STDOUT)
    start = launched + int(args.settle * 1e9)
    end = start + int(args.duration * 1e9)
    _wait_ticks_past(target, end, args.report_ms)
    run.update({
        "window": {"start_ns": start, "end_ns": end, "source": "harness clock after perf launch"},
        "perf_data": str(data.relative_to(out)),
        "events_lost": recorded_loss(data) if recorder.returncode == 0 else None,
        "outside_window_s": seconds - args.duration,
    })
    if recorder.returncode != 0:
        run["error"] = f"perf record exited {recorder.returncode}; see {log.name}"


def one_run(args, out, workload, mode, rnd):
    run = {"workload": workload, "mode": mode, "round": rnd, "started_at": utc_now()}
    target = Target(args.target, workload, args.threads, args.report_ms, args.io_dir)
    run["pid"] = target.pid
    try:
        {"baseline": lambda: run_baseline(target, args, run),
         "performer": lambda: run_performer(target, args, run, out),
         "perf": lambda: run_perf(target, args, run, out)}[mode]()
    except Exception as exc:  # recorded, never silently dropped
        run["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        target.stop()
    window = run.get("window")
    if window:
        run["metrics"] = overhead_bench.window_metrics(target.ticks(), window["start_ns"], window["end_ns"])
    run["ended_at"] = utc_now()
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--target", type=pathlib.Path, default=ROOT / "tests/target/contention")
    parser.add_argument("--perf", default=shutil.which("perf"))
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--settle", type=float, default=2.0)
    parser.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--hz", type=int, default=99)
    parser.add_argument("--profile", default="standard")
    parser.add_argument("--report-ms", type=int, default=100)
    parser.add_argument("--io-dir", type=pathlib.Path, required=True,
                        help="directory on a real block device for the io workload")
    parser.add_argument("--workloads", nargs="+", choices=sorted(WORKLOADS), default=["cpu", "lock", "io"])
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root: bpftrace and perf -p need it")
    if "perf" in args.modes and not args.perf:
        parser.error("perf is not installed; pass --perf or drop the perf mode")
    if not args.target.is_file():
        parser.error("build the Linux target first: make target")
    if "io" in args.workloads and "tmpfs" in _command("findmnt", "-no", "FSTYPE", "--target", str(args.io_dir)):
        parser.error(f"{args.io_dir} is tmpfs; fdatasync would not reach a device")
    args.out.mkdir(parents=True, exist_ok=True)
    results = {"environment": environment(args), "runs": []}
    path = args.out / "results.json"
    for rnd in range(args.rounds):
        modes = list(args.modes)
        modes = modes[rnd % len(modes):] + modes[:rnd % len(modes)]
        for workload in args.workloads:
            for mode in modes:
                run = one_run(args, args.out, workload, mode, rnd)
                results["runs"].append(run)
                path.write_text(json.dumps(results, indent=2) + "\n")
                m = run.get("metrics") or {}
                print(f"round {rnd} {workload:4} {mode:9} "
                      f"{m.get('throughput_per_s', float('nan')):12,.1f}/s "
                      f"{run.get('error', '')}", flush=True)
    overhead_bench.main([str(path), "--json", str(args.out / "summary.json"),
                         "--markdown", str(args.out / "summary.md")])


if __name__ == "__main__":
    main()
