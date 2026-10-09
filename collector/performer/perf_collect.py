"""On-CPU perf capture with optional DWARF unwinding, in the normal bundle format."""
from __future__ import annotations

import datetime as dt
import math
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, provenance, layout, manifest, proc, profiles, symbols, window
from .bundle import BundleBuilder
from .collect import _interrupt_guard, _utc_for_clock
from .errors import PerformerError, PreflightError
from .parse.perf import parse_perf
from .parse.perf_data import recorded_loss
from .parse.stacks import MapEntry, OnCpuLayout, StackKey, fold_oncpu
from .runner import ProbeProcess, TargetWatcher, scan_stderr, wait_for_run


def record_command(binary, pid, hz, call_graph, stack_size, output, ctl, ack):
    unwind = f"dwarf,{stack_size}" if call_graph == "dwarf" else "fp"
    return [binary, "record", "--delay=-1", f"--control=fd:{ctl},{ack}",
            "--clockid", "CLOCK_BOOTTIME", "--buildid-all", "--max-size", "256M",
            "-e", "cpu-clock", "-F", str(hz), "--call-graph", unwind,
            "-p", str(pid), "-o", str(output)]


def control_command(probe, ctl, ack, command, timeout_s=15):
    os.write(ctl, (command + "\n").encode())
    deadline = time.monotonic() + timeout_s
    reply = b""
    while time.monotonic() < deadline:
        if not probe.alive:
            raise PerformerError(f"perf exited before {command} acknowledgement: {probe.read_stderr()[-600:]}")
        readable, _, _ = select.select([ack], [], [], min(.05, max(0, deadline - time.monotonic())))
        if readable:
            reply += os.read(ack, 256)
            if b"ack\n" in reply:
                return
    raise PerformerError(f"perf did not acknowledge {command}; see raw/perf.stderr.log")


def fold_samples(samples, pid, start_ns, end_ns):
    selected = [s for s in samples if s.pid == pid and s.event == "cpu-clock" and start_ns <= s.timestamp_ns < end_ns]
    entries = []
    for sample in selected:
        frames = [f.symbol + ("_[k]" if f.module == "[kernel.kallsyms]" and not f.unknown else "") for f in sample.frames]
        entries.append(MapEntry((StackKey(frames), sample.comm, str(sample.tid)), 1))
    folded, stats = fold_oncpu(entries, layout=OnCpuLayout(kernel=None, user=0, comm=1, tid=2))
    return folded, stats, selected


def collect_perf(*, pid, label, out_dir, duration_s=20.0, oncpu_hz=99,
                 call_graph="dwarf", dwarf_stack_size=8192, perf=None, printer=print):
    profiles.validate_oncpu_hz(oncpu_hz)
    if call_graph not in ("fp", "dwarf"):
        raise PerformerError("call graph must be fp or dwarf")
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or not math.isfinite(duration_s) or not 0 < duration_s <= 600:
        raise PerformerError("perf duration must be greater than zero and at most 600 seconds")
    if isinstance(dwarf_stack_size, bool) or not isinstance(dwarf_stack_size, int) or not 1024 <= dwarf_stack_size <= 65528 or dwarf_stack_size % 8:
        raise PerformerError("DWARF stack size must be a multiple of 8 from 1024 to 65528")
    binary = shutil.which(perf or "perf")
    if binary is None:
        raise PreflightError("perf is missing; install the Linux perf tools or pass --perf")
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"perf version check failed: {exc}") from exc
    if version.returncode:
        raise PreflightError("perf is unusable: " + (version.stderr or version.stdout).strip())
    if not proc.is_running(pid):
        raise PreflightError(f"target PID {pid} is not a live readable process")
    target = proc.target_info(pid)
    modules_start = symbols.modules_snapshot(pid)
    builder = BundleBuilder(out_dir, label=label)
    data = builder.path_for("raw/oncpu.perf.data")
    watcher = TargetWatcher(pid)
    interrupted = threading.Event()
    warnings = ["perf cpu-clock records on-CPU callchains only; no off-CPU, lock or PMU counter measurement"]
    if call_graph == "dwarf":
        warnings.append("DWARF unwinding uses saved user stacks; larger stack dumps and higher sampling rates increase cost")
    printer(f"perf: pid {pid}, {oncpu_hz} Hz, {call_graph} callchains")
    with tempfile.TemporaryDirectory(prefix="performer-perf-control-") as temporary:
        paths = [Path(temporary) / name for name in ("ctl", "ack")]
        fds = []
        probe = None
        try:
            for path in paths:
                os.mkfifo(path, 0o600)
                fds.append(os.open(path, os.O_RDWR | os.O_NONBLOCK))
            ctl, ack = fds
            probe = ProbeProcess("oncpu", record_command(binary, pid, oncpu_hz, call_graph, dwarf_stack_size, data, ctl, ack),
                                 builder.path_for("raw/perf.stdout.log"), builder.path_for("raw/perf.stderr.log"),
                                 env={"DEBUGINFOD_URLS": ""}, pass_fds=fds)
            with _interrupt_guard(interrupted, printer):
                probe.start()
                control_command(probe, ctl, ack, "enable")
                start_ns = window.clock_now_ns()
                started_at = _utc_for_clock(start_ns)
                snapshot_start_ns = window.clock_now_ns()
                threads_start = proc.snapshot_threads(pid)
                snapshot_start_end_ns = window.clock_now_ns()
                watcher.start()
                outcome = wait_for_run(duration_s=duration_s, watcher=watcher, probes=[probe], interrupted=interrupted,
                                       monotonic=lambda: window.clock_now_ns()/1e9, started_at=start_ns/1e9)
                end_ns = min(start_ns + int(duration_s * 1e9), window.clock_now_ns())
                disable_begin = window.clock_now_ns()
                if probe.alive:
                    control_command(probe, ctl, ack, "disable")
                disable_end = window.clock_now_ns()
                snapshot_end_ns = window.clock_now_ns()
                threads_end = proc.snapshot_threads(pid) if proc.is_same_process(pid, watcher.start_time_ticks) else {}
                snapshot_end_end_ns = window.clock_now_ns()
                probe.stop()
        finally:
            watcher.stop()
            if probe is not None:
                probe.stop()
            for fd in fds:
                os.close(fd)
    elapsed = (end_ns - start_ns)/1e9
    ended_at = started_at + dt.timedelta(seconds=elapsed)
    exported = subprocess.run([binary, "script", "--ns", "-i", str(data), "-F", "comm,pid,tid,time,event,ip,sym,dso"],
                              capture_output=True, text=True, env={**os.environ, "DEBUGINFOD_URLS": ""}, timeout=120)
    if exported.returncode:
        raise PerformerError("perf could not decode the preserved recording: " + exported.stderr[-600:])
    samples, parse_warnings = parse_perf(exported.stdout)
    folded, stats, selected = fold_samples(samples, pid, start_ns, end_ns)
    stderr = scan_stderr(probe.read_stderr())
    lost = recorded_loss(data)
    if lost is None:
        warnings.append("perf recording loss could not be determined; do not assume zero dropped samples")
    elif lost:
        warnings.append(f"perf recording contains {lost} lost events/samples; totals are a lower bound")
    warnings.extend(parse_warnings)
    warnings.extend(stderr.warnings)
    empty_chains = sum(not sample.frames for sample in selected)
    if empty_chains:
        warnings.append(f"{empty_chains} CPU samples had no callchain; these are not resolved stacks")
    if outcome.reason != "duration":
        warnings.append(f"perf capture ended early: {outcome.reason}")
    status = "failed" if not folded else "partial" if lost is None or lost or empty_chains or parse_warnings or stderr.events_lost or stderr.has_errors or outcome.reason != "duration" else "ok"
    if probe.exit_info.reason in ("sigkill", "sigterm", "startup_error") or probe.exit_info.exit_code not in (0, -2, 130):
        status = "partial" if folded else "failed"
        warnings.append(f"perf exit {probe.exit_info.reason}, code {probe.exit_info.exit_code}")
    builder.add_folded(layout.STACK_ONCPU, folded)
    builder.add_json("meta/oncpu.frames.json", {"schema_version": 2, "source": "perf", "probe": "oncpu",
        "samples": [{"tid": s.tid, "comm": s.comm, "timestamp_ns": s.timestamp_ns,
                     "frames": [{"address": f.address, "symbol": f.symbol, "module": f.module} for f in s.frames]} for s in selected]})
    build_ids = subprocess.run([binary, "buildid-list", "-i", str(data)], capture_output=True, text=True, timeout=30)
    builder.add_text("meta/perf-buildids.txt", build_ids.stdout)
    if build_ids.returncode:
        warnings.append("perf build-id export failed; preserve raw/oncpu.perf.data for later resolution")
    builder.add_json("meta/modules.json", {"schema_version": 2, "source": "/proc/pid/maps and ELF GNU build-id",
        "start": modules_start, "end": symbols.modules_snapshot(pid) if proc.is_same_process(pid, watcher.start_time_ticks) else []})
    builder.add_json(layout.META_THREADS, {"schema_version": 2, "clk_tck": proc.CLK_TCK,
        "sampled_at_start": manifest.format_ts(started_at + dt.timedelta(seconds=(snapshot_start_ns-start_ns)/1e9)),
        "sampled_at_end": manifest.format_ts(started_at + dt.timedelta(seconds=(snapshot_end_ns-start_ns)/1e9)),
        "threads": proc.merge_thread_snapshots(threads_start, threads_end)})
    builder.add_json(layout.META_SYSTEM, proc.system_info(pid))
    target.update(thread_count_start=len(threads_start), thread_count_end=len(threads_end),
                  died_during_run=watcher.died.is_set())
    builder.add_json(layout.META_TARGET, target)
    builder.add_json(layout.META_WINDOW, {"schema_version": 2, "clock": "boottime", "start_ns": start_ns, "end_ns": end_ns,
        "gate": "individual perf samples filtered after acknowledged enable control", "certified": True,
        "snapshot_start_begin_ns": snapshot_start_ns, "snapshot_start_end_ns": snapshot_start_end_ns,
        "snapshot_end_begin_ns": snapshot_end_ns, "snapshot_end_end_ns": snapshot_end_end_ns,
        "perf_disable_begin_ns": disable_begin, "perf_disable_end_ns": disable_end, "warnings": []})
    builder.set_started_at(started_at)
    quality = manifest.Quality(frame_pointers_ok=stats.total_frames > 0 and stats.unknown_ratio <= .3, unknown_frame_ratio=round(stats.unknown_ratio, 4),
        estimated_overhead_pct=None, unknown_frame_samples=stats.unknown_frames, total_frame_samples=stats.total_frames,
        notes=[f"{call_graph} unwinding; frame pointers were not checked" if call_graph == "dwarf" else "perf frame-pointer unwinding",
               "overhead could not be estimated: this perf-only capture has no paired CPU baseline"])
    document = manifest.build_manifest(label=label, profile="perf", duration_s=duration_s, actual_duration_s=round(elapsed, 6),
        started_at=started_at, ended_at=ended_at,
        target=manifest.TargetInfo(pid, str(target.get("comm") or "unknown"), len(threads_start), len(threads_end), target.get("cmdline"), target.get("exe")),
        probes=[manifest.ProbeResult("oncpu", status=status, duration_s=elapsed, events_lost=max(lost, stderr.events_lost) if lost is not None else None,
                  warnings=warnings, exit_reason=probe.exit_info.reason, exit_code=probe.exit_info.exit_code,
                  thresholds={"sample_hz": oncpu_hz, "unwinder": call_graph}, outputs=[layout.STACK_ONCPU])],
        quality=quality, tool_versions={**provenance.tool_versions(), "perf": version.stdout.strip(), "kernel": proc.system_info().get("kernel")},
        warnings=warnings)
    builder.write_manifest(document)
    return builder.pack()
