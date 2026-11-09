"""Opt-in, system-wide tracing of a loaded module and its trace events.

Configuration contains identifiers, never bpftrace expressions. Function names
must uniquely belong to the module in kallsyms: bpftrace 0.14 cannot qualify a
kprobe with a module name. Request keys are scalar integer trace-event fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from . import profiles
from .errors import PerformerError

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FUNCTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
EVENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}:[A-Za-z_][A-Za-z0-9_]{0,63}$")
INTEGER = re.compile(r"^(?:(?:unsigned|signed)\s+)?(?:char|short(?: int)?|int|long(?: long)?(?: int)?|[us](?:8|16|32|64)|__[us](?:8|16|32|64)|pid_t|size_t|ssize_t|uintptr_t|dma_addr_t)$")


def _identifier(value: Any, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PerformerError(f"kernel config: invalid {label}: {value!r}")
    return value


def validate_config(document: Any) -> Dict[str, Any]:
    if not isinstance(document, dict) or set(document) - {"module", "functions", "tracepoints", "requests"}:
        raise PerformerError("kernel config: expected module, functions, tracepoints and/or requests")
    module = _identifier(document.get("module"), IDENT, "module name")
    functions = document.get("functions", [])
    events = document.get("tracepoints", [])
    requests = document.get("requests", [])
    for value, label, limit in ((functions, "functions", 32), (events, "tracepoints", 16), (requests, "requests", 16)):
        if not isinstance(value, list) or len(value) > limit:
            raise PerformerError(f"kernel config: {label} must be a list of at most {limit} entries")
    functions = [_identifier(fn, FUNCTION, "function") for fn in functions]
    events = [_identifier(event, EVENT, "tracepoint (subsystem:event)") for event in events]
    if len(set(functions)) != len(functions) or len(set(events)) != len(events):
        raise PerformerError("kernel config: duplicate function or tracepoint")
    pairs = []
    used = set(events)
    names = set()
    for pair in requests:
        if not isinstance(pair, dict) or set(pair) != {"name", "start", "end", "key"}:
            raise PerformerError("kernel config: each request needs exactly name, start, end, key")
        item = {
            "name": _identifier(pair["name"], IDENT, "request name"),
            "start": _identifier(pair["start"], EVENT, "start tracepoint"),
            "end": _identifier(pair["end"], EVENT, "end tracepoint"),
            "key": _identifier(pair["key"], IDENT, "request key field"),
        }
        if item["key"] in {"common_type", "common_flags", "common_preempt_count", "common_pid"}:
            raise PerformerError("kernel config: request key must be an event-specific integer field, not a common tracepoint header field")
        if item["name"] in names or item["start"] == item["end"] or {item["start"], item["end"]} & used:
            raise PerformerError("kernel config: request names and start/end tracepoints must be unique")
        names.add(item["name"])
        used.update((item["start"], item["end"]))
        pairs.append(item)
    if not functions and not events and not pairs:
        raise PerformerError("kernel config: select at least one function, tracepoint or request pair")
    return {"module": module, "functions": functions, "tracepoints": events, "requests": pairs}


def check_host(config: Dict[str, Any], *, sys_module: Path = Path("/sys/module"),
               kallsyms: Path = Path("/proc/kallsyms"), tracefs: Optional[Path] = None) -> None:
    module = config["module"]
    if not (sys_module / module).is_dir():
        raise PerformerError(f"kernel module {module!r} is not loaded (/sys/module/{module})")
    if config["functions"]:
        wanted = set(config["functions"])
        owners: Dict[str, list] = {name: [] for name in wanted}
        try:
            for line in kallsyms.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] in wanted and parts[1].lower() == "t":
                    owners[parts[2]].append(parts[3] if len(parts) > 3 else "[vmlinux]")
        except OSError as exc:
            raise PerformerError(f"cannot read kernel symbols: {exc}") from exc
        for name in config["functions"]:
            if owners[name] != [f"[{module}]"]:
                raise PerformerError(f"kernel function {name!r} must uniquely belong to [{module}] in {kallsyms}; found {owners[name]}. Use explicit tracepoints for ambiguous or inlined functions.")
    if not config["tracepoints"] and not config["requests"]:
        return
    if tracefs is None:
        tracefs = next((path for path in (Path("/sys/kernel/tracing"), Path("/sys/kernel/debug/tracing"))
                        if (path / "events").is_dir()), Path("/sys/kernel/tracing"))
    pairs = {pair[side]: pair["key"] for pair in config["requests"] for side in ("start", "end")}
    for event in config["tracepoints"] + list(pairs):
        path = tracefs / "events" / event.replace(":", "/") / "format"
        try:
            fmt = path.read_text()
        except OSError as exc:
            raise PerformerError(f"cannot read tracepoint {event} format at {path}: {exc}") from exc
        key = pairs.get(event)
        if key:
            match = re.search(r"field:\s*([^;]+)\s+" + re.escape(key) + r"\s*;", fmt)
            if match is None or INTEGER.fullmatch(" ".join(match.group(1).split())) is None:
                raise PerformerError(f"tracepoint {event}: {key!r} must be a scalar integer field")


def load_config(path: Path) -> Dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PerformerError(f"cannot read kernel config {path}: {exc}") from exc
    return validate_config(document)


def _event(label: str, phase: int, key: str) -> str:
    # Per-CPU writers avoid a shared non-atomic sequence counter. All CPUs
    # together retain at most ~2048 events; map capacity is also bounded.
    return f'''
    if (@_kernel_event_count[cpu] < {profiles.thread_event_quota()}) {{
        @kernel_event[nsecs, "{label}", {phase}, {key}, pid, tid, cpu] = 1;
        @_kernel_event_count[cpu] = @_kernel_event_count[cpu] + 1;
    }} else {{
        @kernel_events_dropped = count();
    }}
'''


def render(config: Dict[str, Any]) -> str:
    blocks = []
    for index, fn in enumerate(config["functions"]):
        # TID zero is shared by idle tasks: assign each CPU a negative identity.
        context = '$ctx = (int64)tid; if (tid == 0) { $ctx = -1 - (int64)cpu; }'
        blocks.append(f'''
kprobe:{fn}
{{
    {context}
    @kernel_calls["{fn}"] = count();
    $depth = @_kernel_depth[{index}, $ctx] + 1;
    @_kernel_depth[{index}, $ctx] = $depth;
    if ($depth <= 16) {{
        @_kernel_start[{index}, $ctx, $depth] = nsecs;
    }} else {{
        @kernel_depth_overflow["{fn}"] = count();
    }}
}}
kretprobe:{fn}
{{
    {context}
    $depth = @_kernel_depth[{index}, $ctx];
    if ($depth) {{
        $start = @_kernel_start[{index}, $ctx, $depth];
        if ($depth <= 16 && $start && nsecs >= $start) {{
            @kernel_function_ns["{fn}"] = stats(nsecs - $start);
            @kernel_function_max_ns["{fn}"] = max(nsecs - $start);
            delete(@_kernel_start[{index}, $ctx, $depth]);
        }}
        if ($depth == 1) {{ delete(@_kernel_depth[{index}, $ctx]); }}
        else {{ @_kernel_depth[{index}, $ctx] = $depth - 1; }}
    }} else {{
        @kernel_unmatched_returns["{fn}"] = count();
    }}
}}
''')
    for event in config["tracepoints"]:
        blocks.append(f'''tracepoint:{event}
{{
    @kernel_tracepoints["{event}"] = count();
    {_event(event, 0, '0')}
}}
''')
    for index, pair in enumerate(config["requests"]):
        name, key = pair["name"], pair["key"]
        blocks.append(f'''tracepoint:{pair["start"]}
{{
    $id = (uint64)args->{key};
    @kernel_request_started["{name}"] = count();
    if (@_kernel_request_start[{index}, $id]) {{ @kernel_request_reused["{name}"] = count(); }}
    @_kernel_request_start[{index}, $id] = nsecs;
    {_event(name, 1, '$id')}
}}
tracepoint:{pair["end"]}
{{
    $id = (uint64)args->{key};
    $start = @_kernel_request_start[{index}, $id];
    if ($start && nsecs >= $start) {{
        @kernel_request_ns["{name}"] = stats(nsecs - $start);
        @kernel_request_max_ns["{name}"] = max(nsecs - $start);
        delete(@_kernel_request_start[{index}, $id]);
    }} else {{
        @kernel_request_unmatched["{name}"] = count();
    }}
    {_event(name, 2, '$id')}
}}
''')
    return "\n".join(blocks) + '''
interval:ms:100
{
    if (!@_performer_ready) {
        printf("PERFORMER_READY\\n");
        @_performer_ready = 1;
    }
}
interval:s:$2 { exit(); }
'''


def extend_profile(profile: profiles.Profile, config: Dict[str, Any]) -> profiles.Profile:
    if profile.probe("kernel"):
        raise PerformerError("profile already contains a kernel probe")
    spec = profiles.ProbeSpec(name="kernel", program="kernel.bt", source=render(config), required=True)
    return replace(profile, probes=profile.probes + (spec,))


def emit(context: Any, text: str) -> Any:
    from .emit import EmitResult
    from . import layout
    from .parse.hist import parse_maps, table_doc
    from .parse.events import kernel_events
    result = EmitResult()
    if "@kernel_" not in text and "@_performer_start:" not in text:
        return result
    dump = parse_maps(text)
    result.warnings.extend(dump.warnings)
    values = lambda name: dict(dump.values.get(name, []))
    stats = lambda name: dict(dump.stats.get(name, []))
    calls, measured = values("kernel_calls"), stats("kernel_function_ns")
    maximum = values("kernel_function_max_ns")
    unmatched = values("kernel_unmatched_returns")
    overflow = values("kernel_depth_overflow")
    rows = []
    for fn in sorted(set(calls) | set(measured) | set(unmatched)):
        sample = measured.get(fn, {})
        completed = int(sample.get("count", 0))
        total = sample.get("sum", 0)
        rows.append([fn, calls.get(fn, 0), completed, total / 1000,
                     total / completed / 1000 if completed else None,
                     maximum.get(fn, 0) / 1000 if completed else None,
                     max(0, calls.get(fn, 0) - completed), unmatched.get(fn, 0)])
    columns = [
        {"id": "function", "type": "string"}, {"id": "entered", "type": "int"},
        {"id": "completed", "type": "int"}, {"id": "total_us", "type": "float", "unit": "us"},
        {"id": "mean_us", "type": "float", "unit": "us"},
        {"id": "max_us", "type": "float", "unit": "us"},
        {"id": "without_return", "type": "int"}, {"id": "without_entry", "type": "int"},
    ]
    context.builder.add_json(layout.HIST_KERNEL_FUNCTIONS, table_doc("kernel_functions", "kernel.bt; inclusive elapsed time, completed in-window calls only", columns, rows))
    result.outputs.append(layout.HIST_KERNEL_FUNCTIONS)
    started, durations = values("kernel_request_started"), stats("kernel_request_ns")
    reused, missing = values("kernel_request_reused"), values("kernel_request_unmatched")
    maximum = values("kernel_request_max_ns")
    rows = []
    for name in sorted(set(started) | set(durations) | set(missing)):
        sample = durations.get(name, {})
        completed, total = int(sample.get("count", 0)), sample.get("sum", 0)
        rows.append([name, started.get(name, 0), completed, total / 1000,
                     total / completed / 1000 if completed else None,
                     maximum.get(name, 0) / 1000 if completed else None,
                     max(0, started.get(name, 0) - completed), missing.get(name, 0), reused.get(name, 0)])
    request_columns = [dict(column) for column in columns]
    request_columns[0] = {"id": "request", "type": "string"}
    request_columns[6]["id"] = "without_completion"
    request_columns[7]["id"] = "without_start"
    request_columns.append({"id": "reused_id", "type": "int"})
    context.builder.add_json(layout.HIST_KERNEL_REQUESTS, table_doc("kernel_requests", "kernel.bt; request ID correlation across execution contexts", request_columns, rows))
    result.outputs.append(layout.HIST_KERNEL_REQUESTS)
    for name, count in sorted(reused.items()):
        if count:
            result.warnings.append(f"request {name}: {count} IDs reused before completion; request correlation is incomplete")
            result.partial = True
    if any(overflow.values()):
        result.warnings.append("kernel function nesting exceeded 16; some call durations were not retained")
        result.partial = True
    events, warnings, partial = kernel_events(text)
    context.builder.add_json(layout.HIST_KERNEL_EVENTS, events)
    result.outputs.append(layout.HIST_KERNEL_EVENTS)
    result.warnings.extend(warnings)
    result.partial |= partial or bool(dump.warnings)
    counts = [[name, count] for name, count in sorted(values("kernel_tracepoints").items())]
    context.builder.add_json(layout.HIST_KERNEL_TRACEPOINTS, table_doc("kernel_tracepoints", "kernel.bt", [
        {"id": "tracepoint", "type": "string"}, {"id": "count", "type": "int"},
    ], counts))
    result.outputs.append(layout.HIST_KERNEL_TRACEPOINTS)
    return result
