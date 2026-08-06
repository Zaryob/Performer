"""Rendering a run for a human: ``oxfscope inspect``.

The quality thresholds live here as data, not scattered through format
strings, because the viewer applies the same rules in M3 and the two must not
drift.  ``THRESHOLDS`` is the single place to tune them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import layout
from .bundle import Bundle, ValidationReport

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

_LEVEL_ORDER = {LEVEL_ERROR: 0, LEVEL_WARN: 1, LEVEL_INFO: 2}
_LEVEL_MARK = {LEVEL_ERROR: "[X]", LEVEL_WARN: "[!]", LEVEL_INFO: "[i]"}


@dataclass(frozen=True)
class Thresholds:
    #: Above this, flame graphs are actively misleading and the run is red.
    unknown_frame_ratio_error: float = 0.30
    #: Above this the stacks are usable but worth a warning.
    unknown_frame_ratio_warn: float = 0.10
    #: Measurement overhead that starts to change what is being measured.
    overhead_pct_warn: float = 15.0
    overhead_pct_error: float = 30.0


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Flag:
    level: str
    code: str
    message: str
    hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        doc = {"level": self.level, "code": self.code, "message": self.message}
        if self.hint:
            doc["hint"] = self.hint
        return doc


@dataclass
class Summary:
    manifest: Dict[str, Any]
    flags: List[Flag] = field(default_factory=list)
    validation: Optional[ValidationReport] = None

    @property
    def worst_level(self) -> Optional[str]:
        if not self.flags:
            return None
        return min((f.level for f in self.flags), key=lambda lvl: _LEVEL_ORDER[lvl])


def quality_flags(
    doc: Dict[str, Any], thresholds: Thresholds = THRESHOLDS
) -> List[Flag]:
    """Everything that should make an operator distrust this run."""
    flags: List[Flag] = []
    quality = doc.get("quality") or {}
    ratio = quality.get("unknown_frame_ratio")
    frame_pointers_ok = quality.get("frame_pointers_ok")

    if isinstance(ratio, (int, float)) and ratio > thresholds.unknown_frame_ratio_error:
        flags.append(
            Flag(
                LEVEL_ERROR,
                "unknown_frames",
                f"{ratio:.0%} of sampled frames are [unknown]; flame graphs are misleading",
                "Rebuild the target with -fno-omit-frame-pointer, or collect with DWARF unwinding.",
            )
        )
    elif isinstance(ratio, (int, float)) and ratio > thresholds.unknown_frame_ratio_warn:
        flags.append(
            Flag(
                LEVEL_WARN,
                "unknown_frames",
                f"{ratio:.0%} of sampled frames are [unknown]",
                "Deep leaf frames may be attributed to the wrong caller.",
            )
        )
    if frame_pointers_ok is False:
        flags.append(
            Flag(
                LEVEL_ERROR,
                "frame_pointers",
                "preflight decided frame pointers are missing",
                "Rebuild the target with -fno-omit-frame-pointer.",
            )
        )
    if quality.get("ignore_quality"):
        flags.append(
            Flag(
                LEVEL_WARN,
                "ignore_quality",
                "collected with --ignore-quality; the frame pointer check was overridden",
            )
        )

    overhead = quality.get("estimated_overhead_pct")
    if isinstance(overhead, (int, float)):
        if overhead > thresholds.overhead_pct_error:
            flags.append(
                Flag(
                    LEVEL_ERROR,
                    "overhead",
                    f"estimated overhead {overhead:.1f}% -- the measurement changed the workload",
                    "Use a lighter profile or a shorter duration before drawing conclusions.",
                )
            )
        elif overhead > thresholds.overhead_pct_warn:
            flags.append(
                Flag(
                    LEVEL_WARN,
                    "overhead",
                    f"estimated overhead {overhead:.1f}%",
                    "Comparisons against a run with different overhead are unreliable.",
                )
            )

    status = doc.get("status")
    if status == "failed":
        flags.append(Flag(LEVEL_ERROR, "run_failed", "no probe produced usable data"))
    elif status == "partial":
        flags.append(
            Flag(LEVEL_WARN, "run_partial", "run is incomplete; see the probe table")
        )

    if doc.get("target_died_at"):
        flags.append(
            Flag(
                LEVEL_WARN,
                "target_died",
                f"target process exited during the run at {doc['target_died_at']}",
            )
        )

    for probe in doc.get("probes", []):
        if not isinstance(probe, dict):
            continue
        name = probe.get("name", "?")
        if probe.get("status") == "failed":
            flags.append(
                Flag(LEVEL_ERROR, "probe_failed", f"probe '{name}' produced nothing")
            )
        elif probe.get("status") == "partial":
            flags.append(Flag(LEVEL_WARN, "probe_partial", f"probe '{name}' is partial"))
        lost = probe.get("events_lost") or 0
        if lost:
            flags.append(
                Flag(
                    LEVEL_WARN,
                    "events_lost",
                    f"probe '{name}' lost {lost:,} events; its totals are a lower bound",
                    "Raise BPFTRACE_MAP_KEYS_MAX or narrow the probe's filter.",
                )
            )
        if probe.get("exit_reason") == "sigkill":
            flags.append(
                Flag(
                    LEVEL_ERROR,
                    "probe_sigkill",
                    f"probe '{name}' was SIGKILLed, so bpftrace never dumped its maps",
                )
            )

    for warning in doc.get("warnings", []):
        flags.append(Flag(LEVEL_WARN, "run_warning", str(warning)))

    flags.sort(key=lambda f: _LEVEL_ORDER[f.level])
    return flags


def build_summary(bundle: Bundle, *, validate: bool = True) -> Summary:
    report = bundle.validate() if validate else None
    return Summary(
        manifest=bundle.manifest,
        flags=quality_flags(bundle.manifest),
        validation=report,
    )


# --------------------------------------------------------------------------
# text rendering
# --------------------------------------------------------------------------


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _row(label: str, value: str) -> str:
    return f"  {label:<20}{value}"


def render(summary: Summary, *, origin: Optional[str] = None, verbose: bool = False) -> str:
    doc = summary.manifest
    lines: List[str] = []
    target = doc.get("target") or {}
    quality = doc.get("quality") or {}
    tools = doc.get("tool_versions") or {}

    lines.append(f"run {doc.get('run_id', '?')}  [{doc.get('status', '?')}]")
    if origin:
        lines.append(f"  {origin}")
    lines.append("")
    lines.append(_row("label", str(doc.get("label", "?"))))
    tags = doc.get("tags") or []
    if tags:
        lines.append(_row("tags", ", ".join(str(t) for t in tags)))
    lines.append(_row("profile", str(doc.get("profile", "?"))))
    duration = doc.get("actual_duration_s", doc.get("duration_s"))
    requested = doc.get("duration_s")
    duration_text = f"{float(duration):g} s"
    if requested is not None and duration != requested:
        duration_text += f" (requested {float(requested):g} s)"
    lines.append(_row("started / duration", f"{doc.get('started_at', '?')}  {duration_text}"))
    lines.append(
        _row(
            "target",
            f"pid {target.get('pid', '?')}  {target.get('comm', '?')}  "
            f"threads {target.get('thread_count_start', '?')} -> "
            f"{target.get('thread_count_end', '?')}",
        )
    )
    lines.append(
        _row(
            "tooling",
            f"bpftrace {tools.get('bpftrace') or 'n/a'}  |  kernel "
            f"{tools.get('kernel') or 'n/a'}  |  oxfscope {tools.get('oxfscope', '?')}",
        )
    )
    notes = doc.get("notes")
    if notes:
        lines.append(_row("notes", str(notes)))

    probes = doc.get("probes") or []
    if probes:
        lines.append("")
        lines.append("probes")
        width = max(len(str(p.get("name", "?"))) for p in probes)
        for probe in probes:
            lost = probe.get("events_lost") or 0
            detail = []
            if lost:
                detail.append(f"lost {lost:,}")
            if probe.get("exit_reason") and probe.get("exit_reason") != "sigint":
                detail.append(str(probe["exit_reason"]))
            for warning in probe.get("warnings", []):
                detail.append(str(warning))
            suffix = ("  " + "; ".join(detail)) if detail else ""
            lines.append(
                f"  {str(probe.get('name', '?')):<{width}}  "
                f"{str(probe.get('status', '?')):<8}{suffix}"
            )

    lines.append("")
    lines.append("quality")
    ratio = quality.get("unknown_frame_ratio")
    lines.append(
        _row(
            "frame pointers",
            "ok" if quality.get("frame_pointers_ok") else "MISSING",
        )
    )
    if isinstance(ratio, (int, float)):
        lines.append(_row("unknown frames", f"{ratio:.1%}"))
    overhead = quality.get("estimated_overhead_pct")
    if isinstance(overhead, (int, float)):
        lines.append(_row("est. overhead", f"{overhead:.1f}%"))

    files = doc.get("files") or []
    if files:
        lines.append("")
        lines.append(f"artifacts ({len(files)})")
        shown = files if verbose else [f for f in files if not f["path"].startswith("raw/")]
        width = max((len(f["path"]) for f in shown), default=0)
        for entry in shown:
            rows = entry.get("rows")
            extra = f"  {rows:,} rows" if isinstance(rows, int) else ""
            lines.append(
                f"  {entry['path']:<{width}}  {_human_bytes(entry['bytes']):>10}{extra}"
            )
        hidden = len(files) - len(shown)
        if hidden:
            lines.append(f"  ... and {hidden} raw log file(s); use --verbose to list")

    lines.append("")
    if summary.flags:
        lines.append("flags")
        for flag in summary.flags:
            lines.append(f"  {_LEVEL_MARK[flag.level]} {flag.message}")
            if flag.hint:
                lines.append(f"      -> {flag.hint}")
    else:
        lines.append("flags")
        lines.append("  none -- this run looks trustworthy")

    if summary.validation is not None:
        lines.append("")
        report = summary.validation
        if report.ok:
            lines.append(
                f"schema: OK ({len(report.checked)} file(s) validated against "
                f"{layout.schema_dir()})"
            )
        else:
            lines.append("schema: FAILED")
            for problem in report.flat():
                lines.append(f"  - {problem}")

    return "\n".join(lines)


def summary_json(summary: Summary) -> Dict[str, Any]:
    doc = summary.manifest
    validation = summary.validation
    return {
        "run_id": doc.get("run_id"),
        "label": doc.get("label"),
        "status": doc.get("status"),
        "profile": doc.get("profile"),
        "started_at": doc.get("started_at"),
        "duration_s": doc.get("duration_s"),
        "actual_duration_s": doc.get("actual_duration_s"),
        "target": doc.get("target"),
        "quality": doc.get("quality"),
        "probes": doc.get("probes"),
        "flags": [f.to_dict() for f in summary.flags],
        "worst_flag_level": summary.worst_level,
        "schema_valid": None if validation is None else validation.ok,
        "schema_problems": [] if validation is None else validation.flat(),
        "files": doc.get("files", []),
    }


def render_probe_names(doc: Dict[str, Any]) -> Sequence[str]:
    return [str(p.get("name")) for p in doc.get("probes", []) if isinstance(p, dict)]
