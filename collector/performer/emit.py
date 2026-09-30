"""Probe output -> bundle files.

One function per probe, in a registry keyed by probe name.  Adding a probe in
a later milestone means adding a ``.bt`` file, a profile entry and an emitter
here; nothing else has to change.

Every emitter is total about failure: it returns the paths it actually wrote
and the warnings it wants recorded, and it never raises for missing data. Most
probes leave no file when they produce nothing. Threadlife is different: a
successful run with no fork/exit events writes an empty table to distinguish
"no churn" from a missing measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import layout
from .bundle import BundleBuilder
from .parse import hist as hist_parse
from .parse import stacks as stack_parse
from .parse import syscalls as syscall_parse

#: Aggregation tables are capped: a futex table with one row per lock address
#: can hold thousands of entries, and beyond the top few hundred they are
#: noise.  Truncation is recorded in the document.
MAX_TABLE_ROWS = 500


@dataclass
class EmitResult:
    outputs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: Set by emitters that produce stacks, so collect.py can fill in the
    #: manifest's symbolisation quality from the real run rather than from
    #: the two second preflight trial.
    fold_stats: Optional[stack_parse.FoldStats] = None
    #: Free-form numbers worth printing while the run finishes.
    notes: List[str] = field(default_factory=list)


@dataclass
class EmitContext:
    """What an emitter needs besides the probe's stdout."""

    builder: BundleBuilder
    duration_s: float
    annotate_kernel: bool = False


Emitter = Callable[[EmitContext, str], EmitResult]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _emit_folded(
    context: EmitContext,
    text: str,
    *,
    map_name: str,
    path: str,
    label: str,
    value_unit: str = "samples",
) -> EmitResult:
    result = EmitResult()
    folded, stats, warnings = stack_parse.parse_oncpu(
        text, map_name=map_name, annotate_kernel=context.annotate_kernel
    )
    result.warnings.extend(warnings)
    if not folded:
        result.warnings.append(f"produced no {label} stacks")
        return result
    context.builder.add_folded(path, folded)
    result.outputs.append(path)
    result.fold_stats = stats
    result.notes.append(
        f"{path}: {len(folded)} stacks, {stats.total_samples:,} {value_unit}, "
        f"{stats.unknown_ratio:.1%} unknown frames"
    )
    return result


def _emit_histogram(
    context: EmitContext,
    dump: hist_parse.MapDump,
    *,
    map_name: str,
    path: str,
    name: str,
    unit: str,
    source: str,
    extra_series: Optional[List[hist_parse.Series]] = None,
) -> Optional[str]:
    series = list(dump.histograms.get(map_name, []))
    series.extend(extra_series or [])
    if not series:
        return None
    context.builder.add_json(
        path, hist_parse.histogram_doc(name, unit, source, series)
    )
    return path


def _stats_as_series(
    dump: hist_parse.MapDump, map_name: str
) -> List[hist_parse.Series]:
    """Carry ``stats()`` output as bucketless series.

    A per-thread stats map has no buckets, but count/avg/total are exactly
    what the Threads table wants, so it is kept in the same document rather
    than thrown away for not being a histogram.
    """
    return [
        hist_parse.Series(key=key, buckets=[], stats=values)
        for key, values in dump.stats.get(map_name, [])
    ]


# --------------------------------------------------------------------------
# emitters
# --------------------------------------------------------------------------


def emit_oncpu(context: EmitContext, text: str) -> EmitResult:
    return _emit_folded(
        context, text, map_name="cpu", path=layout.STACK_ONCPU, label="on-CPU"
    )


def emit_offcpu(context: EmitContext, text: str) -> EmitResult:
    result = _emit_folded(
        context,
        text,
        map_name="offcpu_us",
        path=layout.STACK_OFFCPU,
        label="off-CPU",
        value_unit="us blocked",
    )
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    path = _emit_histogram(
        context,
        dump,
        map_name="offcpu_hist",
        path=layout.HIST_OFFCPU_DURATION,
        name="offcpu_duration",
        unit="us",
        source="offcpu.bt:@offcpu_hist",
    )
    if path:
        result.outputs.append(path)

    # Task state is recorded rather than filtered (see offcpu.bt), so the
    # split between interruptible and uninterruptible sleep is data the
    # viewer can filter on rather than a decision made at collection time.
    by_state = dump.values.get("offcpu_by_state")
    if by_state:
        rows = [[_task_state_name(key), key, value] for key, value in by_state]
        context.builder.add_json(
            layout.HIST_OFFCPU_BY_STATE,
            hist_parse.table_doc(
                "offcpu_by_state",
                "offcpu.bt:@offcpu_by_state",
                [
                    {"id": "state", "label": "task state", "type": "string"},
                    {"id": "raw", "label": "raw", "type": "string"},
                    {
                        "id": "total_us", "label": "blocked", "type": "int",
                        "unit": "us", "sort": "desc",
                    },
                ],
                rows,
                sort_by=2,
            ),
        )
        result.outputs.append(layout.HIST_OFFCPU_BY_STATE)
    return result


def _task_state_name(raw: str) -> str:
    """Render a sched_switch prev_state bitmask.

    A futex wait is interruptible, which is precisely why the probe records
    the state instead of filtering on it.
    """
    try:
        value = int(raw)
    except ValueError:
        return raw
    if value == 0:
        return "runnable (preempted)"
    names = []
    if value & 0x1:
        names.append("interruptible")
    if value & 0x2:
        names.append("uninterruptible")
    if value & 0x4:
        names.append("stopped")
    if value & 0x10:
        names.append("zombie")
    if value & 0x20:
        names.append("dead")
    return "+".join(names) if names else f"state {value}"


def emit_runqlat(context: EmitContext, text: str) -> EmitResult:
    result = EmitResult()
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)
    path = _emit_histogram(
        context,
        dump,
        map_name="runq_us",
        path=layout.HIST_RUNQLAT,
        name="runqlat",
        unit="us",
        source="runqlat.bt:@runq_us",
        extra_series=_stats_as_series(dump, "runq_by_thread"),
    )
    if path:
        result.outputs.append(path)
    else:
        result.warnings.append("produced no run queue latency samples")
    return result


def emit_futex(context: EmitContext, text: str) -> EmitResult:
    result = _emit_folded(
        context,
        text,
        map_name="futex_us",
        path=layout.STACK_FUTEX,
        label="futex",
        value_unit="us waiting",
    )
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    totals = dict(dump.values.get("futex_by_addr", []))
    counts = dict(dump.values.get("futex_cnt_by_addr", []))
    if totals:
        rows: List[List[Any]] = []
        for key, total_us in totals.items():
            calls = counts.get(key, 0)
            rows.append(
                [
                    _hex_addr(key),
                    total_us,
                    calls,
                    round(total_us / calls, 2) if calls else None,
                ]
            )
        context.builder.add_json(
            layout.HIST_FUTEX_BY_ADDR,
            hist_parse.table_doc(
                "futex_by_addr",
                "futex.bt:@futex_by_addr",
                [
                    {"id": "addr", "label": "uaddr", "type": "hex", "unit": "none"},
                    {
                        "id": "total_us", "label": "total wait", "type": "int",
                        "unit": "us", "sort": "desc",
                    },
                    {"id": "calls", "label": "calls", "type": "int", "unit": "count"},
                    {"id": "avg_us", "label": "avg wait", "type": "float", "unit": "us"},
                ],
                rows,
                limit=MAX_TABLE_ROWS,
                sort_by=1,
            ),
        )
        result.outputs.append(layout.HIST_FUTEX_BY_ADDR)
        share = _dominant_share([row[1] for row in rows])
        if share is not None:
            result.notes.append(
                f"{layout.HIST_FUTEX_BY_ADDR}: {len(rows)} lock addresses, "
                f"hottest holds {share:.0%} of the wait time"
            )

    site_path = _emit_futex_sites(context, text)
    if site_path:
        result.outputs.append(site_path)

    path = _emit_histogram(
        context,
        dump,
        map_name="futex_hist",
        path=layout.HIST_FUTEX_DURATION,
        name="futex_duration",
        unit="us",
        source="futex.bt:@futex_hist",
    )
    if path:
        result.outputs.append(path)
    return result


def _emit_futex_sites(context: EmitContext, text: str) -> Optional[str]:
    """Lock address joined to the call path that waited on it.

    Keyed on ``[uaddr, ustack]`` in the probe, because the join cannot be made
    afterwards: knowing the hottest address and, separately, the hottest call
    path does not establish that they are the same contention.  A hex address
    is an identity, not an answer; this is what turns it into a line of code.
    """
    dump = stack_parse.parse_maps(text)
    entries = dump.entries("futex_site")
    if not entries:
        return None
    # bpftrace can assign different stack IDs to stacks that resolve to the
    # same symbols.  Once symbolised, those are one call path.  Sum both maps
    # by the displayed path before joining them; a dict comprehension would
    # keep only the last count and inflate every per-site average.
    counts: Dict[tuple, int] = {}
    for entry in dump.entries("futex_site_cnt"):
        key = (_scalar(entry), _folded_key(entry, context))
        if None not in key:
            counts[key] = counts.get(key, 0) + entry.value

    totals: Dict[tuple, int] = {}
    for entry in entries:
        addr = _scalar(entry)
        stack = _folded_key(entry, context)
        if addr is None or stack is None:
            continue
        key = (addr, stack)
        totals[key] = totals.get(key, 0) + entry.value

    rows: List[List[Any]] = []
    for (addr, stack), total_us in totals.items():
        calls = counts.get((addr, stack), 0)
        rows.append(
            [
                _hex_addr(addr),
                stack,
                total_us,
                calls,
                round(total_us / calls, 2) if calls else None,
            ]
        )
    if not rows:
        return None

    context.builder.add_json(
        layout.HIST_FUTEX_SITES,
        hist_parse.table_doc(
            "futex_sites",
            "futex.bt:@futex_site",
            [
                {"id": "addr", "label": "uaddr", "type": "hex", "unit": "none"},
                {"id": "stack", "label": "call path", "type": "stack"},
                {
                    "id": "total_us", "label": "total wait", "type": "int",
                    "unit": "us", "sort": "desc",
                },
                {"id": "calls", "label": "calls", "type": "int", "unit": "count"},
                {"id": "avg_us", "label": "avg wait", "type": "float", "unit": "us"},
            ],
            rows,
            limit=MAX_TABLE_ROWS,
            sort_by=2,
        ),
    )
    return layout.HIST_FUTEX_SITES


def _scalar(entry: stack_parse.MapEntry) -> Optional[str]:
    scalars = entry.scalars()
    return scalars[0] if scalars else None


def _folded_key(entry: stack_parse.MapEntry, context: EmitContext) -> Optional[str]:
    """The entry's stack key as a folded, root-first path."""
    stacks = entry.stacks()
    if not stacks:
        return None
    frames = [stack_parse.clean_frame(frame) for frame in reversed(stacks[0].frames)]
    if not frames:
        # bpftrace prints nothing at all when it could not walk the stack, and
        # a row with an empty call path would read as "no code took this lock".
        frames = [stack_parse.UNKNOWN]
    return ";".join(frames)


def _hex_addr(key: str) -> str:
    try:
        return f"0x{int(key):x}"
    except ValueError:
        return key


def _dominant_share(values: List[Any]) -> Optional[float]:
    numbers = [v for v in values if isinstance(v, (int, float))]
    total = sum(numbers)
    return max(numbers) / total if total > 0 else None


def emit_wakeup(context: EmitContext, text: str) -> EmitResult:
    result = EmitResult()
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    edges: List[Dict[str, Any]] = []
    malformed = 0
    for key, count in dump.values.get("wake_cnt", []):
        parts = [part.strip() for part in key.split(",")]
        if len(parts) != 2:
            malformed += 1
            continue
        try:
            edges.append(
                {
                    "from_tid": int(parts[0]),
                    "to_tid": int(parts[1]),
                    "count": count,
                    "total_us": None,
                }
            )
        except ValueError:
            malformed += 1
    if malformed:
        result.warnings.append(f"{malformed} wakeup edges could not be parsed")
    if not edges:
        result.warnings.append("produced no wakeup edges")
        return result

    edges.sort(key=lambda edge: edge["count"], reverse=True)
    total = len(edges)
    truncated = total > layout.MAX_WAKEUP_EDGES
    context.builder.add_json(
        layout.GRAPH_WAKEUP_EDGES,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "source": "wakeup.bt:@wake_cnt",
            "edges": edges[: layout.MAX_WAKEUP_EDGES],
            "truncated": truncated,
            "total_edges": total,
        },
    )
    result.outputs.append(layout.GRAPH_WAKEUP_EDGES)
    hubs = _hub_count(edges)
    result.notes.append(
        f"{layout.GRAPH_WAKEUP_EDGES}: {total} edges, busiest waker accounts "
        f"for {hubs:.0%} of wakeups"
    )
    return result


def _hub_count(edges: List[Dict[str, Any]]) -> float:
    per_waker: Dict[int, int] = {}
    for edge in edges:
        per_waker[edge["from_tid"]] = per_waker.get(edge["from_tid"], 0) + edge["count"]
    total = sum(per_waker.values())
    return max(per_waker.values()) / total if total else 0.0


def emit_syscall_lat(context: EmitContext, text: str) -> EmitResult:
    result = EmitResult()
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    totals = dict(dump.values.get("sc_total_us", []))
    counts = dict(dump.values.get("sc_count", []))
    if not totals:
        result.warnings.append("produced no syscall latency data")
        return result

    table = syscall_parse.load()
    unknown = 0
    rows: List[List[Any]] = []
    for key, total_us in totals.items():
        try:
            number = int(key)
        except ValueError:
            continue
        if not table.known(number):
            unknown += 1
        calls = counts.get(key, 0)
        rows.append(
            [
                table.name(number),
                number,
                total_us,
                calls,
                round(total_us / calls, 2) if calls else None,
            ]
        )
    if unknown:
        result.warnings.append(
            f"{unknown} syscall numbers are not in the mapping ({table.source}); "
            "they appear as syscall_<n>"
        )
    context.builder.add_json(
        layout.HIST_SYSCALL_LATENCY,
        hist_parse.table_doc(
            "syscall_latency",
            f"syscall_lat.bt:@sc_total_us ({table.source})",
            [
                {"id": "syscall", "label": "syscall", "type": "string"},
                {"id": "nr", "label": "nr", "type": "int", "unit": "none"},
                {
                    "id": "total_us", "label": "total", "type": "int",
                    "unit": "us", "sort": "desc",
                },
                {"id": "calls", "label": "calls", "type": "int", "unit": "count"},
                {"id": "avg_us", "label": "avg", "type": "float", "unit": "us"},
            ],
            rows,
            limit=MAX_TABLE_ROWS,
            sort_by=2,
        ),
    )
    result.outputs.append(layout.HIST_SYSCALL_LATENCY)
    return result


def emit_threadlife(context: EmitContext, text: str) -> EmitResult:
    result = EmitResult()
    if not text.strip():
        return result
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    forks = dump.values.get("fork_cnt", [])
    exits = dict(dump.values.get("exit_cnt", []))
    if not forks and not exits:
        result.warnings.append("no threads were created or destroyed during the run")

    names = sorted({key for key, _ in forks} | set(exits))
    fork_by_name = dict(forks)
    duration = max(context.duration_s, 1e-9)
    rows = [
        [
            name,
            fork_by_name.get(name, 0),
            exits.get(name, 0),
            round(fork_by_name.get(name, 0) / duration, 3),
        ]
        for name in names
    ]
    context.builder.add_json(
        layout.HIST_THREADLIFE,
        hist_parse.table_doc(
            "threadlife",
            "threadlife.bt:@fork_cnt,@exit_cnt",
            [
                {"id": "comm", "label": "thread", "type": "string"},
                {
                    "id": "created", "label": "created", "type": "int",
                    "unit": "count", "sort": "desc",
                },
                {"id": "exited", "label": "exited", "type": "int", "unit": "count"},
                {"id": "per_s", "label": "created/s", "type": "float", "unit": "count"},
            ],
            rows,
            sort_by=1,
        ),
    )
    result.outputs.append(layout.HIST_THREADLIFE)

    path = _emit_histogram(
        context,
        dump,
        map_name="thread_lifetime_ms",
        path=layout.HIST_THREAD_LIFETIME,
        name="thread_lifetime",
        unit="ms",
        source="threadlife.bt:@thread_lifetime_ms",
    )
    if path:
        result.outputs.append(path)
    return result


def emit_timers(context: EmitContext, text: str) -> EmitResult:
    result = EmitResult()
    dump = hist_parse.parse_maps(text)
    result.warnings.extend(dump.warnings)

    calls = dump.values.get("timer_calls", [])
    if not calls:
        result.warnings.append("no timer activity observed")
        return result

    # The rate is computed here rather than in the probe, so a run that was
    # cut short still reports the right per-second figure.
    duration = max(context.duration_s, 1e-9)
    rows = [
        [_probe_to_call(key), count, round(count / duration, 2)]
        for key, count in calls
    ]
    context.builder.add_json(
        layout.HIST_TIMERS,
        hist_parse.table_doc(
            "timers",
            "timers.bt:@timer_calls",
            [
                {"id": "call", "label": "call", "type": "string"},
                {
                    "id": "count", "label": "calls", "type": "int",
                    "unit": "count", "sort": "desc",
                },
                {"id": "per_s", "label": "calls/s", "type": "float", "unit": "count"},
            ],
            rows,
            sort_by=1,
        ),
    )
    result.outputs.append(layout.HIST_TIMERS)
    busiest = max(rows, key=lambda row: row[1])
    result.notes.append(
        f"{layout.HIST_TIMERS}: {busiest[0]} at {busiest[2]:g}/s"
    )
    return result


def _probe_to_call(probe: str) -> str:
    """``tracepoint:syscalls:sys_enter_futex`` -> ``futex``."""
    tail = probe.rsplit(":", 1)[-1]
    for prefix in ("sys_enter_", "sys_exit_"):
        if tail.startswith(prefix):
            return tail[len(prefix) :]
    return tail


EMITTERS: Dict[str, Emitter] = {
    "oncpu": emit_oncpu,
    "offcpu": emit_offcpu,
    "runqlat": emit_runqlat,
    "futex": emit_futex,
    "wakeup": emit_wakeup,
    "syscall_lat": emit_syscall_lat,
    "threadlife": emit_threadlife,
    "timers": emit_timers,
}


def emit(probe: str, context: EmitContext, text: str) -> EmitResult:
    emitter = EMITTERS.get(probe)
    if emitter is None:
        return EmitResult(
            warnings=[f"no parser for probe '{probe}'; its output was discarded"]
        )
    return emitter(context, text)
