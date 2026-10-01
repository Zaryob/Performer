"""Manifest construction and validation.

The JSON Schema pins the shape of ``manifest.json``.  It cannot express the
cross field rules that make a manifest *meaningful* -- that ``run_id`` really
does encode ``started_at`` and ``label``, or that a run with a failed probe is
not allowed to call itself ``ok``.  Those live in :func:`semantic_problems`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import layout
from .errors import ManifestError
from .jsonschema import load_schema

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
RUN_ID_TS_FORMAT = "%Y%m%dT%H%M%SZ"

PROBE_STATUSES = ("ok", "partial", "failed", "skipped")
RUN_STATUSES = ("ok", "partial", "failed")


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def format_ts(when: _dt.datetime) -> str:
    """RFC 3339 with a literal Z, which is what the schema demands."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).strftime(TS_FORMAT)


def parse_ts(text: str) -> _dt.datetime:
    cleaned = text.replace("Z", "+00:00")
    return _dt.datetime.fromisoformat(cleaned).astimezone(_dt.timezone.utc)


def make_run_id(label: str, started_at: _dt.datetime) -> str:
    if not layout.LABEL_RE.match(label):
        raise ManifestError(
            f"invalid label {label!r}: use letters, digits, dot, dash or underscore"
        )
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=_dt.timezone.utc)
    stamp = started_at.astimezone(_dt.timezone.utc).strftime(RUN_ID_TS_FORMAT)
    return f"{stamp}-{label}"


# --------------------------------------------------------------------------
# pieces
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """One probe's outcome, as it appears in ``manifest.probes[]``."""

    name: str
    status: str = "ok"
    events_lost: int = 0
    warnings: List[str] = field(default_factory=list)
    duration_s: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_code: Optional[int] = None
    thresholds: Optional[Dict[str, Any]] = None
    outputs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "events_lost": self.events_lost,
            "warnings": list(self.warnings),
        }
        if self.duration_s is not None:
            doc["duration_s"] = self.duration_s
        if self.exit_reason is not None:
            doc["exit_reason"] = self.exit_reason
        if self.exit_code is not None:
            doc["exit_code"] = self.exit_code
        if self.thresholds:
            doc["thresholds"] = dict(self.thresholds)
        if self.outputs:
            doc["outputs"] = list(self.outputs)
        return doc


@dataclass
class TargetInfo:
    pid: int
    comm: str
    thread_count_start: int = 0
    thread_count_end: int = 0
    cmdline: Optional[List[str]] = None
    exe: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "pid": self.pid,
            "comm": self.comm,
            "thread_count_start": self.thread_count_start,
            "thread_count_end": self.thread_count_end,
        }
        if self.cmdline is not None:
            doc["cmdline"] = list(self.cmdline)
        if self.exe is not None:
            doc["exe"] = self.exe
        return doc


@dataclass
class Quality:
    frame_pointers_ok: bool = True
    unknown_frame_ratio: float = 0.0
    estimated_overhead_pct: Optional[float] = None
    unknown_frame_samples: Optional[int] = None
    total_frame_samples: Optional[int] = None
    overhead: Optional[Dict[str, float]] = None
    ignore_quality: Optional[bool] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "frame_pointers_ok": self.frame_pointers_ok,
            "unknown_frame_ratio": self.unknown_frame_ratio,
            "estimated_overhead_pct": self.estimated_overhead_pct,
        }
        if self.unknown_frame_samples is not None:
            doc["unknown_frame_samples"] = self.unknown_frame_samples
        if self.total_frame_samples is not None:
            doc["total_frame_samples"] = self.total_frame_samples
        if self.overhead is not None:
            doc["overhead"] = dict(self.overhead)
        if self.ignore_quality is not None:
            doc["ignore_quality"] = self.ignore_quality
        if self.notes:
            doc["notes"] = list(self.notes)
        return doc


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def derive_status(probes: Sequence[ProbeResult], target_died: bool) -> str:
    """Run status implied by the probe outcomes.

    A run is only ``ok`` when every requested probe delivered.  ``failed``
    means nothing usable came out at all; anything in between is ``partial``,
    which is what the viewer badges as "read this with care".
    """
    considered = [p for p in probes if p.status != "skipped"]
    if not considered:
        return "failed"
    if all(p.status == "failed" for p in considered):
        return "failed"
    degraded = target_died or any(
        p.status != "ok" or p.events_lost > 0 for p in considered
    )
    return "partial" if degraded else "ok"


def build_manifest(
    *,
    label: str,
    profile: str,
    duration_s: float,
    target: TargetInfo,
    probes: Sequence[ProbeResult],
    quality: Quality,
    tool_versions: Dict[str, Optional[str]],
    started_at: Optional[_dt.datetime] = None,
    ended_at: Optional[_dt.datetime] = None,
    actual_duration_s: Optional[float] = None,
    tags: Sequence[str] = (),
    notes: str = "",
    target_died_at: Optional[_dt.datetime] = None,
    status: Optional[str] = None,
    files: Optional[List[Dict[str, Any]]] = None,
    warnings: Sequence[str] = (),
) -> Dict[str, Any]:
    """Assemble a manifest document.  Does not validate; callers do that."""
    started = started_at or utc_now()
    doc: Dict[str, Any] = {
        "schema_version": layout.SCHEMA_VERSION,
        "run_id": make_run_id(label, started),
        "label": label,
        "started_at": format_ts(started),
        "duration_s": duration_s,
        "profile": profile,
        "status": status
        or derive_status(probes, target_died=target_died_at is not None),
        "target": target.to_dict(),
        "probes": [p.to_dict() for p in probes],
        "tool_versions": dict(tool_versions),
        "quality": quality.to_dict(),
    }
    if tags:
        doc["tags"] = list(tags)
    if notes:
        doc["notes"] = notes
    if ended_at is not None:
        doc["ended_at"] = format_ts(ended_at)
    if actual_duration_s is not None:
        doc["actual_duration_s"] = actual_duration_s
    if target_died_at is not None:
        doc["target_died_at"] = format_ts(target_died_at)
    if files is not None:
        doc["files"] = files
    if warnings:
        doc["warnings"] = list(warnings)
    return doc


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def schema_problems(doc: Any) -> List[str]:
    validator = load_schema(layout.schema_path(layout.SCHEMA_FOR_PATH[layout.MANIFEST]))
    return [str(err) for err in validator.validate(doc)]


def semantic_problems(doc: Dict[str, Any]) -> List[str]:
    """Cross field rules the schema cannot express."""
    problems: List[str] = []
    if not isinstance(doc, dict):
        return ["manifest is not an object"]

    run_id = doc.get("run_id")
    label = doc.get("label")
    started_at = doc.get("started_at")

    if isinstance(run_id, str) and isinstance(label, str):
        if not run_id.endswith(f"-{label}"):
            problems.append(f"run_id {run_id!r} does not end with label {label!r}")
    if isinstance(run_id, str) and isinstance(started_at, str):
        try:
            expected = parse_ts(started_at).strftime(RUN_ID_TS_FORMAT)
        except ValueError:
            expected = None
        if expected is not None and not run_id.startswith(expected):
            problems.append(
                f"run_id timestamp does not match started_at ({run_id} vs {expected})"
            )

    ended_at = doc.get("ended_at")
    if isinstance(started_at, str) and isinstance(ended_at, str):
        try:
            if parse_ts(ended_at) < parse_ts(started_at):
                problems.append("ended_at is before started_at")
        except ValueError:
            problems.append("started_at/ended_at are not parseable timestamps")

    probes = doc.get("probes")
    if isinstance(probes, list):
        names = [p.get("name") for p in probes if isinstance(p, dict)]
        duplicates = sorted({n for n in names if names.count(n) > 1 and n})
        for name in duplicates:
            problems.append(f"duplicate probe entry {name!r}")

        status = doc.get("status")
        expected_status = derive_status(
            [
                ProbeResult(
                    name=str(p.get("name", "?")),
                    status=str(p.get("status", "ok")),
                    events_lost=int(p.get("events_lost", 0) or 0),
                )
                for p in probes
                if isinstance(p, dict)
            ],
            target_died=doc.get("target_died_at") is not None,
        )
        if status == "ok" and expected_status != "ok":
            problems.append(
                "status is 'ok' but a probe failed, lost events or the target died; "
                f"expected {expected_status!r}"
            )

    if doc.get("target_died_at") is not None and doc.get("status") == "ok":
        problems.append("target_died_at is set but status is 'ok'")

    quality = doc.get("quality")
    if isinstance(quality, dict):
        ratio = quality.get("unknown_frame_ratio")
        unknown = quality.get("unknown_frame_samples")
        total = quality.get("total_frame_samples")
        if isinstance(unknown, int) and isinstance(total, int):
            if unknown > total:
                problems.append("unknown_frame_samples exceeds total_frame_samples")
            elif total > 0 and isinstance(ratio, (int, float)):
                if abs(ratio - unknown / total) > 0.01:
                    problems.append(
                        "unknown_frame_ratio disagrees with the sample counts"
                    )

    files = doc.get("files")
    if isinstance(files, list):
        seen = set()
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str):
                if not layout.is_safe_relpath(path):
                    problems.append(f"unsafe file path {path!r}")
                if path in seen:
                    problems.append(f"duplicate file entry {path!r}")
                seen.add(path)

    return problems


def validate_manifest(doc: Any, *, strict_semantics: bool = True) -> List[str]:
    """Return every problem found.  Empty list means the manifest is valid."""
    problems = schema_problems(doc)
    if not problems and strict_semantics and isinstance(doc, dict):
        problems.extend(semantic_problems(doc))
    return problems


def require_valid(doc: Any, *, source: str = "manifest.json") -> None:
    problems = validate_manifest(doc)
    if problems:
        raise ManifestError(f"{source} is not a valid manifest", problems)
