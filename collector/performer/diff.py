"""Comparing two run bundles.

The viewer has a Diff screen and this is the same computation on the command
line, for the case where the answer is wanted in a terminal, in a log, or in
a script -- and for the case where the bundle is on a machine that has no
browser, which is most of them.

It is a deliberate reimplementation of ``viewer/src/bundle/diff.ts`` rather
than a different idea about what a diff is.  Two tools that disagree about
which call path grew would be worse than having only one, so the two follow
the same rules in the same order: normalise every frame, join the runs on the
resulting path, divide by each run's total, subtract, and keep the paths that
exist on only one side apart from the rest.  ``tests/test_diff.py`` checks the
two against the same fixtures.

Threads are compared by *name*, not by tid.  Tids are not stable across runs:
restart the process and every one of them changes, so a per-tid comparison
reports the whole pool destroyed and recreated.  Names are what a thread pool
has in common between runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import layout, report
from .bundle import Bundle
from .parse.stacks import clean_frame

#: The stack files worth comparing, and what their values mean.  Mirrors
#: ``STACK_KINDS`` in the viewer.
STACK_KINDS: Tuple[Tuple[str, str, str, str], ...] = (
    ("oncpu", layout.STACK_ONCPU, "on-CPU", "samples"),
    ("offcpu", layout.STACK_OFFCPU, "off-CPU", "us blocked"),
    ("futex", layout.STACK_FUTEX, "futex", "us waiting"),
    ("offwake", layout.STACK_OFFWAKE, "off-CPU + waker", "us blocked"),
)

DEFAULT_MIN_SHARE = 0.0001
DEFAULT_TOP = 20


# --------------------------------------------------------------------------
# stacks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PathDelta:
    """One call path, in both runs."""

    path: str
    a: float
    b: float

    @property
    def delta(self) -> float:
        return self.b - self.a

    @property
    def ratio(self) -> Optional[float]:
        """Relative change, or ``None`` when the path is new in B.

        ``None`` is not missing data.  It is the statement "this path did not
        exist before", which is a stronger finding than any percentage and
        must not be flattened into one.
        """
        if self.a <= 0:
            return None
        return (self.b - self.a) / self.a

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "a": self.a,
            "b": self.b,
            "delta": self.delta,
            "ratio": self.ratio,
        }


@dataclass
class StackDiff:
    kind: str
    unit: str
    paths: List[PathDelta] = field(default_factory=list)
    only_in_a: List[PathDelta] = field(default_factory=list)
    only_in_b: List[PathDelta] = field(default_factory=list)
    raw_total_a: int = 0
    raw_total_b: int = 0
    normalised: bool = True
    grew: float = 0.0
    shrank: float = 0.0
    below_threshold: int = 0

    def top(self, count: int) -> List[PathDelta]:
        return self.paths[:count]

    def to_dict(self, *, top: int = DEFAULT_TOP) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "normalised": self.normalised,
            "raw_total_a": self.raw_total_a,
            "raw_total_b": self.raw_total_b,
            "paths_compared": len(self.paths),
            "below_threshold": self.below_threshold,
            "grew": self.grew,
            "shrank": self.shrank,
            "movers": [entry.to_dict() for entry in self.top(top)],
            "only_in_b": [entry.to_dict() for entry in self.only_in_b[:top]],
            "only_in_a": [entry.to_dict() for entry in self.only_in_a[:top]],
        }


def normalise_path(stack: str, *, merge_threads: bool) -> str:
    """Reduce a folded stack to the key the two runs are joined on."""
    frames = stack.split(";")
    if merge_threads and len(frames) > 1:
        frames = frames[1:]
    return ";".join(clean_frame(frame) for frame in frames)


def _fold(
    entries: Iterable[Tuple[str, int]],
    *,
    merge_threads: bool,
    thread_filter: str = "",
) -> Tuple[Dict[str, int], int]:
    needle = thread_filter.strip().lower()
    totals: Dict[str, int] = {}
    total = 0
    for stack, value in entries:
        if needle:
            root = stack.split(";", 1)[0]
            if needle not in root.lower():
                continue
        key = normalise_path(stack, merge_threads=merge_threads)
        totals[key] = totals.get(key, 0) + value
        total += value
    return totals, total


def diff_stacks(
    a_entries: Iterable[Tuple[str, int]],
    b_entries: Iterable[Tuple[str, int]],
    *,
    kind: str = "oncpu",
    unit: str = "samples",
    normalise: bool = True,
    merge_threads: bool = True,
    thread_filter: str = "",
    min_share: float = DEFAULT_MIN_SHARE,
) -> StackDiff:
    left, total_a = _fold(a_entries, merge_threads=merge_threads, thread_filter=thread_filter)
    right, total_b = _fold(b_entries, merge_threads=merge_threads, thread_filter=thread_filter)

    # Dividing by the total is what makes two runs of different lengths
    # comparable at all.  Without it a 120 s run "grew" in every single path
    # against a 60 s one, which is the classic way to read a diff backwards.
    scale_a = 1.0 / total_a if normalise and total_a > 0 else 1.0
    scale_b = 1.0 / total_b if normalise and total_b > 0 else 1.0

    result = StackDiff(
        kind=kind,
        unit=unit,
        raw_total_a=total_a,
        raw_total_b=total_b,
        normalised=normalise,
    )

    for path in set(left) | set(right):
        raw_a = left.get(path, 0)
        raw_b = right.get(path, 0)
        if min_share > 0:
            share_a = raw_a / total_a if total_a else 0.0
            share_b = raw_b / total_b if total_b else 0.0
            if share_a < min_share and share_b < min_share:
                result.below_threshold += 1
                continue
        entry = PathDelta(path=path, a=raw_a * scale_a, b=raw_b * scale_b)
        result.paths.append(entry)
        if entry.delta > 0:
            result.grew += entry.delta
        else:
            result.shrank -= entry.delta
        if raw_a == 0 and raw_b > 0:
            result.only_in_b.append(entry)
        elif raw_b == 0 and raw_a > 0:
            result.only_in_a.append(entry)

    result.paths.sort(key=lambda e: (-abs(e.delta), e.path))
    result.only_in_b.sort(key=lambda e: (-e.b, e.path))
    result.only_in_a.sort(key=lambda e: (-e.a, e.path))
    return result


# --------------------------------------------------------------------------
# threads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadGroup:
    name: str
    count: int
    cpu_ms_per_s: float
    wait_ms_per_s: float


@dataclass(frozen=True)
class ThreadGroupDelta:
    name: str
    count_a: int
    count_b: int
    cpu_a: float
    cpu_b: float
    wait_a: float
    wait_b: float

    @property
    def count_delta(self) -> int:
        return self.count_b - self.count_a

    @property
    def cpu_delta(self) -> float:
        return self.cpu_b - self.cpu_a

    @property
    def wait_delta(self) -> float:
        return self.wait_b - self.wait_a

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "count_a": self.count_a,
            "count_b": self.count_b,
            "count_delta": self.count_delta,
            "cpu_ms_per_s_a": round(self.cpu_a, 3),
            "cpu_ms_per_s_b": round(self.cpu_b, 3),
            "cpu_delta": round(self.cpu_delta, 3),
            "runq_ms_per_s_a": round(self.wait_a, 3),
            "runq_ms_per_s_b": round(self.wait_b, 3),
            "runq_delta": round(self.wait_delta, 3),
        }


def _schedstat_delta(entry: Dict[str, Any], field_name: str) -> float:
    start = (entry.get("start_schedstat") or {}).get(field_name) or 0
    end = (entry.get("end_schedstat") or {}).get(field_name) or 0
    # A thread that appeared mid-run has no start sample, so its end value is
    # already the whole of its life and the difference is right either way.
    return max(0.0, float(end) - float(start))


def group_threads(doc: Optional[Dict[str, Any]], seconds: float) -> Dict[str, ThreadGroup]:
    """Sum every thread sharing a name, per second of run."""
    groups: Dict[str, Dict[str, float]] = {}
    if not doc:
        return {}
    scale = 1.0 / seconds if seconds > 0 else 0.0
    for entry in (doc.get("threads") or {}).values():
        name = str(entry.get("name", "?"))
        bucket = groups.setdefault(name, {"count": 0.0, "cpu": 0.0, "wait": 0.0})
        bucket["count"] += 1
        bucket["cpu"] += _schedstat_delta(entry, "run_ns") / 1e6 * scale
        bucket["wait"] += _schedstat_delta(entry, "wait_ns") / 1e6 * scale
    return {
        name: ThreadGroup(
            name=name,
            count=int(bucket["count"]),
            cpu_ms_per_s=bucket["cpu"],
            wait_ms_per_s=bucket["wait"],
        )
        for name, bucket in groups.items()
    }


def diff_threads(
    doc_a: Optional[Dict[str, Any]],
    seconds_a: float,
    doc_b: Optional[Dict[str, Any]],
    seconds_b: float,
) -> List[ThreadGroupDelta]:
    left = group_threads(doc_a, seconds_a)
    right = group_threads(doc_b, seconds_b)
    rows = []
    for name in set(left) | set(right):
        x = left.get(name)
        y = right.get(name)
        rows.append(
            ThreadGroupDelta(
                name=name,
                count_a=x.count if x else 0,
                count_b=y.count if y else 0,
                cpu_a=x.cpu_ms_per_s if x else 0.0,
                cpu_b=y.cpu_ms_per_s if y else 0.0,
                wait_a=x.wait_ms_per_s if x else 0.0,
                wait_b=y.wait_ms_per_s if y else 0.0,
            )
        )
    rows.sort(key=lambda r: (-abs(r.wait_delta), -abs(r.cpu_delta), r.name))
    return rows


# --------------------------------------------------------------------------
# whole runs
# --------------------------------------------------------------------------


@dataclass
class RunDiff:
    manifest_a: Dict[str, Any]
    manifest_b: Dict[str, Any]
    stacks: List[StackDiff] = field(default_factory=list)
    threads: List[ThreadGroupDelta] = field(default_factory=list)
    #: Reasons the two runs may not be comparable.  Not errors: an operator
    #: may well mean to compare across a rebuild.  But finding out afterwards
    #: that half the difference was the tooling is expensive.
    caveats: List[str] = field(default_factory=list)

    def to_dict(self, *, top: int = DEFAULT_TOP) -> Dict[str, Any]:
        return {
            "a": _run_summary(self.manifest_a),
            "b": _run_summary(self.manifest_b),
            "caveats": self.caveats,
            "stacks": [entry.to_dict(top=top) for entry in self.stacks],
            "threads": [row.to_dict() for row in self.threads[:top]],
        }


def _duration(manifest: Dict[str, Any]) -> float:
    value = manifest.get("actual_duration_s")
    if value is None:
        value = manifest.get("duration_s") or 0
    return float(value)


def _run_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    target = manifest.get("target") or {}
    quality = manifest.get("quality") or {}
    return {
        "run_id": manifest.get("run_id"),
        "label": manifest.get("label"),
        "profile": manifest.get("profile"),
        "status": manifest.get("status"),
        "duration_s": _duration(manifest),
        "pid": target.get("pid"),
        "comm": target.get("comm"),
        "thread_count_start": target.get("thread_count_start"),
        "thread_count_end": target.get("thread_count_end"),
        "unknown_frame_ratio": quality.get("unknown_frame_ratio"),
        "estimated_overhead_pct": report.usable_overhead_pct(quality),
    }


def _caveats(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if a.get("profile") != b.get("profile"):
        notes.append(
            f"different collection profiles ({a.get('profile')} vs {b.get('profile')}); "
            "probes sample at different rates, so some of the difference is the tooling"
        )
    target_a = (a.get("target") or {}).get("comm")
    target_b = (b.get("target") or {}).get("comm")
    if target_a != target_b:
        notes.append(f"different target processes ({target_a} vs {target_b})")
    for manifest in (a, b):
        if manifest.get("status") != "ok":
            notes.append(
                f"{manifest.get('label')} is a {manifest.get('status')} run; "
                "a path missing from it may be missing data rather than missing work"
            )
    for manifest, side in ((a, "A"), (b, "B")):
        ratio = (manifest.get("quality") or {}).get("unknown_frame_ratio") or 0
        if ratio >= 0.30:
            notes.append(
                f"{side} ({manifest.get('label')}) has {ratio * 100:.0f}% unresolved frames; "
                "a diff of unresolved stacks is a confident picture of nothing"
            )
    overhead_a = report.usable_overhead_pct(a.get("quality") or {})
    overhead_b = report.usable_overhead_pct(b.get("quality") or {})
    if (
        overhead_a is not None
        and overhead_b is not None
        and abs(overhead_a - overhead_b) > 10
    ):
        notes.append(
            f"measurement overhead differed a lot ({overhead_a:.0f}% vs {overhead_b:.0f}%); "
            "part of what changed may be the cost of measuring"
        )
    return notes


def compare(
    bundle_a: Bundle,
    bundle_b: Bundle,
    *,
    kinds: Optional[Sequence[str]] = None,
    normalise: bool = True,
    merge_threads: bool = True,
    thread_filter: str = "",
    min_share: float = DEFAULT_MIN_SHARE,
) -> RunDiff:
    manifest_a = bundle_a.manifest
    manifest_b = bundle_b.manifest
    result = RunDiff(
        manifest_a=manifest_a,
        manifest_b=manifest_b,
        caveats=_caveats(manifest_a, manifest_b),
    )

    wanted = set(kinds) if kinds else None
    for kind, path, _label, unit in STACK_KINDS:
        if wanted is not None and kind not in wanted:
            continue
        if not (bundle_a.exists(path) and bundle_b.exists(path)):
            continue
        result.stacks.append(
            diff_stacks(
                bundle_a.iter_folded(path),
                bundle_b.iter_folded(path),
                kind=kind,
                unit=unit,
                normalise=normalise,
                merge_threads=merge_threads,
                thread_filter=thread_filter,
                min_share=min_share,
            )
        )

    threads_a = (
        bundle_a.read_json(layout.META_THREADS)
        if bundle_a.exists(layout.META_THREADS)
        else None
    )
    threads_b = (
        bundle_b.read_json(layout.META_THREADS)
        if bundle_b.exists(layout.META_THREADS)
        else None
    )
    result.threads = diff_threads(
        threads_a, _duration(manifest_a), threads_b, _duration(manifest_b)
    )
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _value(value: float, normalised: bool) -> str:
    if not normalised:
        return f"{value:,.0f}"
    pct = abs(value) * 100
    sign = "-" if value < 0 else ""
    if pct and pct < 0.01:
        return f"{sign}<0.01%"
    return f"{sign}{pct:.2f}%"


def _delta(value: float, normalised: bool) -> str:
    if value == 0:
        return "0"
    return ("+" if value > 0 else "-") + _value(abs(value), normalised)


def _ratio(entry: PathDelta) -> str:
    ratio = entry.ratio
    if ratio is None:
        return "new"
    if entry.b <= 0:
        return "gone"
    if math.isinf(ratio):  # pragma: no cover - guarded by entry.a > 0
        return "inf"
    return f"{ratio * 100:+.0f}%"


def _shorten(path: str, width: int) -> str:
    """Keep the leaf and as much of its caller chain as fits.

    Truncating from the right would keep `app;start_thread;WorkerThread::run`
    for every row and throw away the only part that differs.
    """
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3) :]


def render(diff: RunDiff, *, top: int = DEFAULT_TOP, path_width: int = 78) -> str:
    a = _run_summary(diff.manifest_a)
    b = _run_summary(diff.manifest_b)
    lines: List[str] = []

    lines.append(f"A  {a['label']}  {a['run_id']}  [{a['status']}]")
    lines.append(f"B  {b['label']}  {b['run_id']}  [{b['status']}]")
    lines.append("")
    lines.append(
        f"   duration   {a['duration_s']:g} s -> {b['duration_s']:g} s"
        f"      profile  {a['profile']} -> {b['profile']}"
    )
    lines.append(
        f"   threads    {a['thread_count_start']} -> {b['thread_count_start']} at start,"
        f"  {a['thread_count_end']} -> {b['thread_count_end']} at end"
    )

    for note in diff.caveats:
        lines.append(f"   [!] {note}")

    for stack in diff.stacks:
        lines.append("")
        basis = "share of each run" if stack.normalised else f"raw {stack.unit}"
        lines.append(
            f"{stack.kind}  ({stack.raw_total_a:,} -> {stack.raw_total_b:,} {stack.unit},"
            f" compared as {basis})"
        )
        lines.append(
            f"   {len(stack.paths):,} paths compared"
            + (
                f", {stack.below_threshold:,} below the noise floor"
                if stack.below_threshold
                else ""
            )
            + f"; {_value(stack.grew, stack.normalised)} grew,"
            f" {_value(stack.shrank, stack.normalised)} shrank"
        )
        if not stack.paths:
            lines.append("   (nothing to compare)")
            continue
        lines.append("")
        lines.append(
            f"   {'A':>8}  {'B':>8}  {'delta':>9}  {'rel':>6}  call path (leaf last)"
        )
        for entry in stack.top(top):
            lines.append(
                f"   {_value(entry.a, stack.normalised):>8}"
                f"  {_value(entry.b, stack.normalised):>8}"
                f"  {_delta(entry.delta, stack.normalised):>9}"
                f"  {_ratio(entry):>6}  {_shorten(entry.path, path_width)}"
            )
        for title, rows, side in (
            ("only in B (new call paths)", stack.only_in_b, "b"),
            ("only in A (gone from B)", stack.only_in_a, "a"),
        ):
            if not rows:
                continue
            lines.append("")
            lines.append(f"   {title}: {len(rows):,}")
            for entry in rows[: max(3, top // 2)]:
                value = entry.b if side == "b" else entry.a
                lines.append(
                    f"   {_value(value, stack.normalised):>8}"
                    f"  {'':>8}  {'':>9}  {'':>6}  {_shorten(entry.path, path_width)}"
                )

    if diff.threads:
        lines.append("")
        lines.append("threads by name (per second of run; tids are not stable across runs)")
        lines.append(
            f"   {'threads':>13}  {'CPU ms/s':>16}  {'runq ms/s':>16}  name"
        )
        for row in diff.threads[:top]:
            lines.append(
                f"   {row.count_a:>5} -> {row.count_b:<5}"
                f"  {row.cpu_a:>6.1f} -> {row.cpu_b:<7.1f}"
                f"  {row.wait_a:>6.1f} -> {row.wait_b:<7.1f}  {row.name}"
            )
    return "\n".join(lines)
