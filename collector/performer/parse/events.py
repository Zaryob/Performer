"""Bounded event maps, preserving nanoseconds as decimal strings in JSON."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .hist import parse_maps, table_doc


def origin_ns(text: str, times: List[int]) -> int:
    match = re.search(r"^@_performer_start:\s*(\d+)\s*$", text, re.M)
    return int(match.group(1)) if match else min(times, default=0)


def thread_events(text: str) -> Tuple[Dict[str, Any], List[str], bool]:
    dump = parse_maps(text)
    warnings = []
    parsed = []
    for key, _value in dump.values.get("thread_event", []):
        # The name can contain commas. Numeric fields surround it so split
        # only at known boundaries; the final field is the birth timestamp.
        match = re.fullmatch(r"(\d+), ([12]), (\d+), (\d+), (.*), (\d+)", key)
        if match is None:
            warnings.append("malformed thread lifecycle event was discarded")
            continue
        ts, kind, tid, parent = (int(match.group(i)) for i in range(1, 5))
        born = int(match.group(6))
        if tid <= 0 or (born and born > ts):
            warnings.append("invalid thread lifecycle identity or timestamp was discarded")
            continue
        parsed.append((ts, kind, tid, parent, match.group(5), born))
    parsed.sort(key=lambda item: (item[0], item[1], item[2]))
    retained = re.findall(r"^@_event_count\[\d+\]:\s*(\d+)\s*$", text, re.M)
    if retained and sum(map(int, retained)) != len(parsed):
        warnings.append("thread event map does not match retained-event counters; some records are missing")
    origin = origin_ns(text, [row[0] for row in parsed])
    rows = [[str(ts), (ts - origin) / 1e6, "created" if kind == 1 else "exited",
             tid, parent or None, name, str(born) if born else None,
             (ts - born) / 1e6 if kind == 2 and born else None]
            for ts, kind, tid, parent, name, born in parsed]
    dropped = sum(value for _key, value in dump.values.get("thread_events_dropped", []))
    partial = dropped > 0 or bool(warnings)
    if dropped:
        warnings.append(f"{dropped} thread lifecycle events dropped by the per-CPU event budget; counts/histograms continue, event list is incomplete")
    doc = table_doc("thread_events", "threadlife.bt:@thread_event; CLOCK_BOOTTIME", [
        {"id": "timestamp_ns", "label": "BOOTTIME ns", "type": "string", "unit": "ns"},
        {"id": "offset_ms", "label": "Since capture start (ms)", "type": "float", "unit": "ms"},
        {"id": "event", "type": "string"}, {"id": "tid", "type": "int"},
        {"id": "parent_tid", "type": "int"}, {"id": "comm", "type": "string"},
        {"id": "birth_ns", "label": "Observed generation", "type": "string", "unit": "ns"},
        {"id": "lifetime_ms", "type": "float", "unit": "ms"},
    ], rows)
    if partial:
        doc["truncated"] = True
    return doc, warnings, partial


def kernel_events(text: str) -> Tuple[Dict[str, Any], List[str], bool]:
    dump = parse_maps(text)
    rows = []
    warnings = []
    for key, _value in dump.values.get("kernel_event", []):
        parts = key.split(", ")
        if len(parts) != 7 or not all(parts[index].isdigit() for index in (0, 2, 3, 4, 5, 6)):
            warnings.append("malformed kernel event was discarded")
            continue
        ts, label, phase, request, pid, tid, cpu = parts
        if phase not in ("0", "1", "2"):
            warnings.append("invalid kernel event phase was discarded")
            continue
        rows.append([ts, 0.0, label, {"0": "event", "1": "start", "2": "end"}[phase],
                     request if phase != "0" else None, int(pid), int(tid), int(cpu)])
    rows.sort(key=lambda row: (int(row[0]), row[2], row[3]))
    retained = re.findall(r"^@_kernel_event_count\[\d+\]:\s*(\d+)\s*$", text, re.M)
    if retained and sum(map(int, retained)) != len(rows):
        warnings.append("kernel event map does not match retained-event counters; some records are missing")
    origin = origin_ns(text, [int(row[0]) for row in rows])
    for row in rows:
        row[1] = (int(row[0]) - origin) / 1e6
    dropped = sum(value for _key, value in dump.values.get("kernel_events_dropped", []))
    partial = bool(warnings) or dropped > 0
    if dropped:
        warnings.append(f"{dropped} kernel events dropped by the per-CPU event budget; aggregates continue, event list is incomplete")
    doc = table_doc("kernel_events", "kernel.bt:@kernel_event; CLOCK_BOOTTIME", [
        {"id": "timestamp_ns", "type": "string", "unit": "ns"},
        {"id": "offset_ms", "type": "float", "unit": "ms"},
        {"id": "name", "type": "string"}, {"id": "phase", "type": "string"},
        {"id": "request_id", "type": "string"}, {"id": "pid", "type": "int"},
        {"id": "tid", "type": "int"}, {"id": "cpu", "type": "int"},
    ], rows)
    if partial:
        doc["truncated"] = True
    return doc, warnings, partial
