#!/usr/bin/env python3
"""Summarise alternating baseline/profiled runs of the contention target.

This is the offline half of ``tests/docker/overhead-bench.py``: it reads the
``results.json`` that script writes and does not start or trace anything.

Each run records the target's own ``tick <clock_ns> <iterations> <cpu_ns>``
lines (``--report-ms``). Throughput and CPU per iteration are computed inside
an exact window: Performer's certified BOOTTIME gate for profiled runs, and a
window of the same length at the same settle offset for baseline and perf runs.
Probe attachment, preflight trials and map dumping happen outside that window
and are reported separately as wall-clock costs, not hidden in the ratio.

``python3 tests/overhead_bench.py results.json --markdown summary.md``
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Tick = Tuple[int, int, int]  # clock_ns, iterations, process cpu_ns


def parse_ticks(lines: Iterable[str]) -> List[Tick]:
    ticks: List[Tick] = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and parts[0] == "tick":
            try:
                ticks.append((int(parts[1]), int(parts[2]), int(parts[3])))
            except ValueError:
                continue
    return ticks


def _at(ticks: Sequence[Tick], t_ns: int) -> Optional[Tuple[float, float]]:
    """Iterations and CPU at ``t_ns`` by linear interpolation between ticks.

    Outside the recorded ticks there is nothing to interpolate from, so the
    answer is unknown rather than an extrapolation.
    """
    if not ticks or t_ns < ticks[0][0] or t_ns > ticks[-1][0]:
        return None
    for (t0, i0, c0), (t1, i1, c1) in zip(ticks, ticks[1:]):
        if t0 <= t_ns <= t1:
            if t1 == t0:
                return float(i0), float(c0)
            f = (t_ns - t0) / (t1 - t0)
            return i0 + f * (i1 - i0), c0 + f * (c1 - c0)
    last = ticks[-1]
    return (float(last[1]), float(last[2])) if t_ns == last[0] else None


def window_metrics(ticks: Sequence[Tick], start_ns: int, end_ns: int) -> Optional[Dict[str, float]]:
    if end_ns <= start_ns:
        return None
    a, b = _at(ticks, start_ns), _at(ticks, end_ns)
    if a is None or b is None:
        return None
    seconds = (end_ns - start_ns) / 1e9
    iterations = b[0] - a[0]
    cpu_s = (b[1] - a[1]) / 1e9
    return {
        "window_s": seconds,
        "iterations": iterations,
        "throughput_per_s": iterations / seconds,
        "cpu_cores": cpu_s / seconds,
        "cpu_us_per_iteration": cpu_s * 1e6 / iterations if iterations > 0 else None,
    }


def _stats(values: List[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    ordered = sorted(values)
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive") if len(ordered) > 1 else [ordered[0]] * 3
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "q1": quartiles[0],
        "q3": quartiles[2],
        "min": ordered[0],
        "max": ordered[-1],
    }


def summarise(results: Dict[str, Any]) -> Dict[str, Any]:
    """Per workload and mode: distributions, change against baseline, failures."""
    out: Dict[str, Any] = {}
    for run in results.get("runs", []):
        group = out.setdefault(run["workload"], {}).setdefault(run["mode"], {
            "throughput": [], "cpu_us_per_iteration": [], "failed": [], "events_lost": [],
            "outside_window_s": [],
        })
        metrics = run.get("metrics")
        if run.get("error") or not metrics:
            group["failed"].append({"round": run["round"], "error": run.get("error") or "no window metrics"})
            continue
        group["throughput"].append(metrics["throughput_per_s"])
        if metrics.get("cpu_us_per_iteration") is not None:
            group["cpu_us_per_iteration"].append(metrics["cpu_us_per_iteration"])
        if run.get("events_lost") is not None:
            group["events_lost"].append(run["events_lost"])
        if run.get("outside_window_s") is not None:
            group["outside_window_s"].append(run["outside_window_s"])

    summary: Dict[str, Any] = {}
    for workload, modes in out.items():
        base = _stats(modes.get("baseline", {}).get("throughput", []))
        base_cpu = _stats(modes.get("baseline", {}).get("cpu_us_per_iteration", []))
        rows = {}
        for mode, group in modes.items():
            tput = _stats(group["throughput"])
            cpu = _stats(group["cpu_us_per_iteration"])
            row: Dict[str, Any] = {
                "throughput": tput,
                "cpu_us_per_iteration": cpu,
                "failed_runs": group["failed"],
                "events_lost_total": sum(group["events_lost"]) if group["events_lost"] else None,
                "outside_window_s": _stats(group["outside_window_s"]),
            }
            if mode != "baseline" and base and tput and base["median"] > 0:
                row["throughput_change_pct"] = (tput["median"] / base["median"] - 1) * 100
            if mode != "baseline" and base_cpu and cpu and base_cpu["median"] > 0:
                row["cpu_per_iteration_change_pct"] = (cpu["median"] / base_cpu["median"] - 1) * 100
            rows[mode] = row
        summary[workload] = rows
    return summary


def _fmt(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    return "—" if value is None else f"{value:,.{digits}f}{suffix}"


def _signed(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def markdown(results: Dict[str, Any], summary: Dict[str, Any]) -> str:
    lines = ["| Workload | Mode | Runs ok/failed | Throughput/s median [IQR] | Δ throughput | "
             "CPU µs/iter median | Δ CPU/iter | Events lost |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    order = ["baseline", "performer", "perf"]
    for workload, rows in summary.items():
        for mode in sorted(rows, key=lambda m: order.index(m) if m in order else len(order)):
            row = rows[mode]
            t = row["throughput"]
            c = row["cpu_us_per_iteration"]
            ok = t["n"] if t else 0
            lines.append(
                f"| {workload} | {mode} | {ok}/{len(row['failed_runs'])} | "
                + (f"{t['median']:,.0f} [{t['q1']:,.0f}–{t['q3']:,.0f}]" if t else "—")
                + f" | {_signed(row.get('throughput_change_pct'))} | "
                + _fmt(c["median"] if c else None, 2)
                + f" | {_signed(row.get('cpu_per_iteration_change_pct'))} | "
                + ("—" if row["events_lost_total"] is None else str(row["events_lost_total"]))
                + " |"
            )
    failures = [
        f"- {workload}/{mode} round {f['round']}: {f['error']}"
        for workload, rows in summary.items() for mode, row in rows.items() for f in row["failed_runs"]
    ]
    if failures:
        lines += ["", "Failed runs:", *failures]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path)
    parser.add_argument("--json", type=Path, help="write the summary as JSON")
    parser.add_argument("--markdown", type=Path, help="write the summary table as Markdown")
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text())
    summary = summarise(results)
    table = markdown(results, summary)
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(table)
    sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
