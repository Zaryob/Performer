#!/usr/bin/env python3
"""Compare a supplied Performer bundle with a Linux ``perf script`` export.

This is an offline evidence tool: it does not start, stop, or trace processes.
Create a time-based reference with ``perf record -p PID -e cpu-clock -F 99
--clockid CLOCK_BOOTTIME --call-graph fp -o reference.data -- sleep 20``;
``dwarf,16384`` is an alternate
unwinder for binaries without frame pointers. Export it with ``perf script
-i reference.data -F comm,pid,tid,time,event,ip,sym,dso > reference.txt``.

Run ``python3 tests/perf_compare.py run.tgz reference.txt --perf-meta meta.json``.
The optional JSON metadata must describe the *actual recording window*, not
when perf was launched: pid, event, sample_hz, call_graph, started_at, ended_at,
and clock ("boottime" with the command above). UTC timestamps describe
the recording bounds; sample timestamps in the text use the selected clock.
Example: {"pid": 123, "event": "cpu-clock", "sample_hz": 99,
"call_graph": "fp", "clock": "boottime", "started_at":
"2026-10-04T10:00:00Z", "ended_at": "2026-10-04T10:00:20Z"}.

Equal rates do not imply identical samples: perf cpu-clock and bpftrace's
per-CPU profile timer use independent time-based sampling. Off-CPU intervals
are not comparable to cpu-clock samples. Do not infer missing idle threads or
overhead from these sample counts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

COLLECTOR = Path(__file__).resolve().parents[1] / "collector"
if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))

from performer import layout
from performer.bundle import Bundle
from performer.errors import PerformerError

from performer.parse.perf import PerfFrame, PerfSample, is_unknown, parse_perf, timestamp_ns

_TID = re.compile(r"\s+\[tid=([0-9]+)\]$")

def frame_quality(total: int, unknown: int, empty: int) -> Dict[str, Any]:
    return {
        "sampled_frame_occurrences": total,
        "unknown_frame_occurrences": unknown,
        "unknown_frame_ratio": unknown / total if total else None,
        "samples_without_frames": empty,
        "denominator": "stack-frame occurrences weighted by sample count; thread roots excluded",
    }


def summarise_folded(entries):
    counts: Counter = Counter()
    frames = unknown = empty = unassigned = total = 0
    for stack, weight in entries:
        if weight < 0:
            raise ValueError("folded sample weights cannot be negative")
        path = stack.split(";")
        match = _TID.search(path[0])
        if match:
            tid = int(match[1])
            counts[tid] += weight
        else:
            unassigned += weight
        # All shipped folded stacks include the process/thread root, even
        # old versions lacking its TID. It is not a sampled stack frame.
        sampled = path[1:]
        total += weight
        frames += weight * len(sampled)
        unknown += weight * sum(is_unknown(name) for name in sampled)
        if not sampled:
            empty += weight
    return counts, total, unassigned, frame_quality(frames, unknown, empty)


def cpu_delta_ms(entry: Dict[str, Any]) -> Optional[float]:
    start = (entry.get("start_schedstat") or {}).get("run_ns")
    end = (entry.get("end_schedstat") or {}).get("run_ns")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           or v < 0 for v in (start, end)):
        return None
    return (end - start) / 1e6 if end >= start else None


def utc_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        return timestamp.timestamp()
    except (ValueError, OverflowError):
        return None


def compare(bundle: Bundle, perf_text: str, perf_meta=None, *, tolerance_s=0.25):
    """Return JSON-ready evidence; mismatches remain explicit, never repaired."""
    meta = {} if perf_meta is None else perf_meta
    if not isinstance(meta, dict):
        raise ValueError("perf metadata must be a JSON object")
    validation = bundle.validate(verify_hashes=True)
    if not validation.ok:
        raise ValueError("invalid Performer bundle: " + "; ".join(validation.flat()))
    manifest = bundle.manifest
    pid = manifest["target"]["pid"]
    samples, parse_warnings = parse_perf(perf_text)
    selected = [sample for sample in samples if sample.event.split(":", 1)[0] == "cpu-clock"]
    target_samples = [sample for sample in selected if sample.pid == pid]
    window_doc = bundle.read_json("meta/window.json") if bundle.exists("meta/window.json") else {}
    expected_clock = window_doc.get("clock")
    perf_clock = meta.get("clock")
    comparison_window = {"mode": "untrimmed", "clock": expected_clock, "samples_outside_window": 0}
    offset = meta.get("boottime_minus_monotonic_s")
    calibrated = perf_clock == "monotonic" and isinstance(offset, (int, float)) and not isinstance(offset, bool) and math.isfinite(offset)
    if expected_clock == "boottime" and (perf_clock == "boottime" or calibrated):
        gate_start, gate_end = window_doc.get("start_ns"), window_doc.get("end_ns")
        if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (gate_start, gate_end)) and gate_end >= gate_start:
            shift_ns = round(offset * 1e9) if calibrated else 0
            retained = [s for s in target_samples if gate_start <= s.timestamp_ns + shift_ns < gate_end]
            comparison_window.update(mode="performer_gate", start_ns=gate_start, end_ns=gate_end,
                                     samples_outside_window=len(target_samples) - len(retained))
            if calibrated:
                comparison_window["boottime_minus_monotonic_s"] = offset
            target_samples = retained
    perf_counts = Counter(sample.tid for sample in target_samples)
    folded = list(bundle.iter_folded(layout.STACK_ONCPU)) if bundle.exists(layout.STACK_ONCPU) else []
    performer_counts, total, unassigned, quality = summarise_folded(folded)
    offcpu = list(bundle.iter_folded(layout.STACK_OFFCPU)) if bundle.exists(layout.STACK_OFFCPU) else []
    offcpu_counts, offcpu_us, offcpu_unassigned, _ = summarise_folded(offcpu)
    threads_doc = bundle.read_json(layout.META_THREADS) if bundle.exists(layout.META_THREADS) else {}
    inventory = threads_doc.get("threads", {})
    oncpu = next((p for p in manifest["probes"] if p["name"] == "oncpu"), {})
    hz = (oncpu.get("thresholds") or {}).get("sample_hz")
    perf_frames = [frame for sample in target_samples for frame in sample.frames]
    perf_quality = frame_quality(len(perf_frames), sum(frame.unknown for frame in perf_frames),
                                 sum(not sample.frames for sample in target_samples))
    checks: List[Dict[str, Any]] = []

    def check(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    check("oncpu_probe", "matched" if oncpu.get("status") == "ok" else "advisory",
          f"Performer oncpu status {oncpu.get('status', 'missing')}; reported events lost {oncpu.get('events_lost', 'unknown')}")
    if window_doc:
        certification = window_doc.get("certified")
        check("performer_gate", "matched" if certification is True else "mismatch" if certification is False else "unknown",
              f"shared gate certified {certification!r}; probe status still determines whether the full window was observed")
    if meta.get("simultaneous_recording") is True:
        check("overhead", "advisory",
              "Concurrent perf recording contaminates the collector's overhead estimate; run a separate Performer-only capture for overhead sanity.")
    observed_pids = sorted({sample.pid for sample in selected})
    if selected and observed_pids != [pid]:
        check("pid", "mismatch", f"bundle PID {pid}; perf cpu-clock PIDs {observed_pids}; other PIDs excluded")
    elif meta.get("pid") is not None and meta["pid"] != pid:
        check("pid", "mismatch", f"bundle PID {pid}; perf metadata PID {meta['pid']}")
    elif selected:
        check("pid", "matched", f"both sources contain PID {pid}")
    else:
        check("pid", "unknown", "no parseable cpu-clock reference samples")
    if hz is None or meta.get("sample_hz") is None:
        check("sample_hz", "unknown", "both effective sampling frequencies must be recorded")
    else:
        check("sample_hz", "matched" if hz == meta["sample_hz"] else "mismatch",
              f"Performer {hz} Hz; perf {meta['sample_hz']} Hz")
    observed_events = sorted({sample.event for sample in selected})
    if any(event != "cpu-clock" for event in observed_events) or meta.get("event") not in (None, "cpu-clock"):
        check("event", "mismatch", f"expected unqualified cpu-clock; export {observed_events}; metadata {meta.get('event')!r}")
    elif target_samples:
        check("event", "matched", "time-based cpu-clock reference; independent of bpftrace profile timer")
    else:
        check("event", "unknown", "no cpu-clock reference samples; cycles/instructions are not a matched reference")
    unwind = meta.get("call_graph")
    performer_unwind = (oncpu.get("thresholds") or {}).get("unwinder", "fp")
    check("unwinder", "matched" if unwind == performer_unwind else "advisory" if unwind else "unknown",
          f"Performer {performer_unwind}; perf " + (str(unwind) if unwind else "unwinder not supplied"))
    check("perf_clock", "advisory" if calibrated and expected_clock == "boottime"
          else "matched" if expected_clock is not None and perf_clock == expected_clock
          else "unknown" if perf_clock is None or expected_clock is None else "mismatch",
          f"Performer gate clock {expected_clock!r}; perf sample clock {perf_clock!r}; supplied calibration {offset!r}; sample timestamps are not UTC")
    start = utc_seconds(manifest.get("started_at"))
    end = utc_seconds(manifest.get("ended_at"))
    pstart, pend = utc_seconds(meta.get("started_at")), utc_seconds(meta.get("ended_at"))
    window = {"performer_started_at": manifest.get("started_at"), "performer_ended_at": manifest.get("ended_at"),
              "perf_started_at": meta.get("started_at"), "perf_ended_at": meta.get("ended_at")}
    if None in (start, end, pstart, pend):
        check("recording_window", "unknown", "actual UTC recording bounds are required for both sources")
    elif end < start or pend < pstart:
        check("recording_window", "mismatch", "recording end precedes start")
    else:
        window["overlap_s"] = max(0, min(end, pend) - max(start, pstart))
        window["start_difference_s"] = abs(start - pstart)
        window["end_difference_s"] = abs(end - pend)
        window["capture_windows_differ"] = max(abs(start - pstart), abs(end - pend)) > tolerance_s
        if comparison_window["mode"] == "performer_gate":
            check("recording_window", "matched" if pstart <= start + tolerance_s and pend >= end - tolerance_s else "mismatch",
                  "perf samples clipped to Performer gate; " +
                  ("original perf capture encloses that window" if pstart <= start + tolerance_s and pend >= end - tolerance_s
                   else "perf capture does not cover the full gate"))
        else:
            check("recording_window", "matched" if not window["capture_windows_differ"] else "mismatch",
                  f"start differs by {abs(start-pstart):.3f}s; end by {abs(end-pend):.3f}s; tolerance {tolerance_s}s")
    all_tids = sorted(set(map(int, inventory)) | set(performer_counts) | set(perf_counts) | set(offcpu_counts))
    rows = []
    perf_names = {sample.tid: sample.comm for sample in target_samples}
    for tid in all_tids:
        entry = inventory.get(str(tid), {})
        rows.append({"tid": tid, "name": entry.get("name", perf_names.get(tid, "")),
                     "in_performer_inventory": str(tid) in inventory,
                     "performer_oncpu_samples": performer_counts[tid], "perf_oncpu_samples": perf_counts[tid],
                     "performer_offcpu_us": offcpu_counts[tid],
                     "proc_cpu_ms": cpu_delta_ms(entry),
                     "only_performer_samples": performer_counts[tid] > 0 and perf_counts[tid] == 0,
                     "only_perf_samples": perf_counts[tid] > 0 and performer_counts[tid] == 0})
    unresolved = sorted({(frame.address, frame.symbol, frame.module) for frame in perf_frames if frame.unknown})
    return {
        "schema_version": 1, "run_id": bundle.run_id, "target_pid": pid,
        "comparability": {"checks": checks, "has_mismatch": any(c["status"] == "mismatch" for c in checks),
                          "has_unknown": any(c["status"] == "unknown" for c in checks)},
        "recording_window": window,
        "comparison_window": comparison_window,
        "simultaneous_recording": meta.get("simultaneous_recording"),
        "performer": {"oncpu_samples": total, "oncpu_tids": sum(n > 0 for n in performer_counts.values()),
                      "inventory_tids": len(inventory), "samples_without_tid": unassigned, "quality": quality,
                      "oncpu_duration_s": oncpu.get("duration_s"), "oncpu_status": oncpu.get("status"),
                      "offcpu_us": offcpu_us, "offcpu_tids": sum(n > 0 for n in offcpu_counts.values()),
                      "offcpu_us_without_tid": offcpu_unassigned,
                      "offcpu_warnings": [w for p in manifest["probes"] if p["name"] == "offcpu" for w in p.get("warnings", [])]},
        "perf": {"export_samples": len(samples), "target_cpu_clock_samples": len(target_samples),
                 "excluded_samples": len(samples) - len(target_samples), "oncpu_tids": len(perf_counts), "quality": perf_quality,
                 "sample_time_first_s": min((s.timestamp_s for s in target_samples), default=None),
                 "sample_time_last_s": max((s.timestamp_s for s in target_samples), default=None),
                 "sample_time_first_ns": min((s.timestamp_ns for s in target_samples), default=None),
                 "sample_time_last_ns": max((s.timestamp_ns for s in target_samples), default=None),
                 "unresolved_frames": [{"address": a, "symbol": s, "module": m} for a, s, m in unresolved]},
        "threads": rows, "parse_warnings": parse_warnings,
        "caveats": [
            "Inventory is a union of endpoint snapshots, not the roster of every thread that existed during collection.",
            "Zero samples do not imply zero CPU time; samples from the two independent timers need not select identical TIDs.",
            "Unknown ratio counts sampled stack-frame occurrences, not unique functions or percent of CPU; empty callchains are reported separately.",
            "Performer /proc CPU deltas are independent counters; endpoint snapshots may cover a different interval in older bundles.",
            "No perf off-CPU comparison: cpu-clock measures running time. Performer open waits are right-censored; pre-attachment sleeps have no observed switch-out stack.",
            "Sample counts cannot establish profiler overhead; concurrent profiling changes the observed workload.",
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("perf_script", type=Path)
    parser.add_argument("--perf-meta", type=Path, help="actual perf recording bounds and settings as JSON")
    parser.add_argument("--json", type=Path, help="also write the complete evidence report to this file")
    parser.add_argument("--window-tolerance-s", type=float, default=0.25)
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.window_tolerance_s) or args.window_tolerance_s < 0:
            raise ValueError("window tolerance must be a finite nonnegative number")
        metadata = json.loads(args.perf_meta.read_text()) if args.perf_meta else None
        with Bundle.open(args.bundle) as bundle:
            report = compare(bundle, args.perf_script.read_text(), metadata, tolerance_s=args.window_tolerance_s)
        output = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.json:
            args.json.write_text(output)
        print(output, end="")
    except (OSError, ValueError, PerformerError) as exc:
        print(f"perf_compare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
