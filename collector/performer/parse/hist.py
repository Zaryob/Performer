"""bpftrace ``hist()``/``lhist()``/``stats()`` output -> normalised documents.

bpftrace prints a histogram as a header line and one line per bucket::

    @runq_us:
    [0]                    3 |@@@                                             |
    [1]                   12 |@@@@@@@@@@@                                     |
    [2, 4)                 8 |@@@@@@@                                         |
    [1K, 2K)               4 |@@@                                             |

and a keyed one with the key in the header: ``@runq_by_thread[worker0]:``.

Three shapes come out of the same scan, because a single probe prints all
three and they interleave:

    histograms  ``hist()`` and ``lhist()``      -> hist.schema.json
    values      ``count()``/``sum()`` maps       -> table.schema.json
    stats       ``stats()``                      -> table.schema.json

The ASCII bar is ignored -- it is a rendering of the count, not data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import layout

#: ``@name:`` or ``@name[key]:`` at column zero opens a map.
_HEADER_RE = re.compile(r"^@([A-Za-z_][A-Za-z0-9_]*)?(?:\[(.*)\])?:\s*(.*)$")

#: ``@name[`` with nothing closing it on the same line opens a stack keyed
#: map, whose key spans several lines.  Those belong to parse/stacks.py; this
#: parser has to recognise and skip them, not complain about them, because a
#: single probe prints both shapes into one stream.
#:
#: The key may begin with scalars before the stack -- ``@futex_site[140737,``
#: joins a lock address to the call path that waited on it -- so what marks
#: the line is the *absence* of a closing ``]:``, not an empty key.
_STACK_MAP_START_RE = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*\[(?!.*\]:).*$")
_STACK_MAP_END_RE = re.compile(r"\]:\s*\S")

#: ``[2, 4)  8 |@@@|`` -- the bracket kinds vary between hist and lhist.
_BUCKET_RE = re.compile(r"^\s*([\[(])([^\])]*)([\])])\s+(\d+)\s*(\|.*)?$")

#: ``count 5, average 3, total 15``
_STATS_RE = re.compile(r"^\s*(count|average|total|min|max|sum)\s+(-?\d+)")

_SI = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_NUMBER_RE = re.compile(r"^(-?\d+)([KMGT]?)$")


@dataclass
class Bucket:
    lo: Optional[float]
    hi: Optional[float]
    count: int

    def to_dict(self) -> Dict[str, Any]:
        return {"lo": self.lo, "hi": self.hi, "count": self.count}


@dataclass
class Series:
    key: str
    buckets: List[Bucket] = field(default_factory=list)
    stats: Optional[Dict[str, float]] = None

    @property
    def total_count(self) -> int:
        return sum(b.count for b in self.buckets)

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "key": self.key,
            "buckets": [b.to_dict() for b in self.buckets],
            "total_count": self.total_count,
        }
        if self.stats:
            doc["stats"] = dict(self.stats)
        return doc


@dataclass
class MapDump:
    """Everything one probe printed, split by shape."""

    histograms: Dict[str, List[Series]] = field(default_factory=dict)
    values: Dict[str, List[Tuple[str, int]]] = field(default_factory=dict)
    stats: Dict[str, List[Tuple[str, Dict[str, float]]]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    preamble: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.histograms or self.values or self.stats)


def _parse_bound(token: str) -> Optional[float]:
    """``1K`` -> 1024, ``...`` -> None (an open ended bucket edge)."""
    token = token.strip()
    if not token or token == "...":
        return None
    match = _NUMBER_RE.match(token)
    if match is None:
        raise ValueError(f"unparseable bucket bound {token!r}")
    return float(match.group(1)) * _SI[match.group(2)]


def _parse_bucket(open_bracket: str, body: str, close_bracket: str, count: int) -> Bucket:
    if "," in body:
        lo_text, _, hi_text = body.partition(",")
        lo = _parse_bound(lo_text)
        hi = _parse_bound(hi_text)
        # "(..., 0)" is lhist's underflow bucket. Its lower edge is genuinely
        # open, so it is reported as null rather than being pinned to zero --
        # a bucket whose lo equals its hi would claim to span nothing while
        # holding samples.
        return Bucket(lo=lo, hi=hi, count=count)
    # A single value bucket: "[0]" and "[1]" are the exact-value buckets
    # bpftrace emits at the bottom of a power-of-two histogram.
    value = _parse_bound(body)
    if value is None:
        raise ValueError(f"unparseable bucket {open_bracket}{body}{close_bracket}")
    return Bucket(lo=value, hi=value + 1, count=count)


def _parse_stats(text: str) -> Optional[Dict[str, float]]:
    parts = [p.strip() for p in text.split(",")]
    found: Dict[str, float] = {}
    for part in parts:
        match = _STATS_RE.match(part)
        if match is None:
            return None
        # count is a cardinality and the schema types it as an integer;
        # average and total stay floating point.
        raw = int(match.group(2))
        found[match.group(1)] = raw if match.group(1) == "count" else float(raw)
    if not found:
        return None
    # bpftrace names it "average"; the schema (and everyone else) says "avg".
    if "average" in found:
        found["avg"] = found.pop("average")
    if "total" in found and "sum" not in found:
        found["sum"] = found.pop("total")
    return found


def parse_maps(text: str) -> MapDump:
    """Scan a probe's stdout for every map it printed."""
    dump = MapDump()
    lines = text.splitlines()
    index = 0
    current_name: Optional[str] = None
    current_series: Optional[Series] = None

    def flush() -> None:
        nonlocal current_name, current_series
        if current_name is not None and current_series is not None:
            if current_series.buckets:
                dump.histograms.setdefault(current_name, []).append(current_series)
            # A header with no buckets is an empty map, not an error: the probe
            # attached and simply saw nothing worth recording.
        current_name = None
        current_series = None

    while index < len(lines):
        line = lines[index]
        index += 1

        bucket_match = _BUCKET_RE.match(line)
        if bucket_match and current_series is not None:
            try:
                current_series.buckets.append(
                    _parse_bucket(
                        bucket_match.group(1),
                        bucket_match.group(2),
                        bucket_match.group(3),
                        int(bucket_match.group(4)),
                    )
                )
            except ValueError as exc:
                dump.warnings.append(f"@{current_name}: {exc}")
            continue

        if not line.startswith("@"):
            if line.strip() and current_series is None:
                dump.preamble.append(line.strip())
            continue

        flush()

        if _STACK_MAP_START_RE.match(line):
            # A stack keyed map: skip to its terminator and leave it to the
            # stack parser.
            while index < len(lines) and not _STACK_MAP_END_RE.search(lines[index]):
                index += 1
            index += 1
            continue

        header = _HEADER_RE.match(line)
        if header is None:
            dump.warnings.append(f"unrecognised map line: {line.strip()[:120]}")
            continue

        name = header.group(1) or ""
        key = header.group(2) or ""
        trailer = header.group(3).strip()

        if not trailer:
            # A histogram: the buckets follow on subsequent lines.
            current_name = name
            current_series = Series(key=key)
            continue

        stats = _parse_stats(trailer)
        if stats is not None:
            dump.stats.setdefault(name, []).append((key, stats))
            continue

        try:
            dump.values.setdefault(name, []).append((key, int(trailer)))
        except ValueError:
            dump.warnings.append(
                f"@{name}: value {trailer[:60]!r} is not a plain number"
            )

    flush()
    return dump


# --------------------------------------------------------------------------
# bundle documents
# --------------------------------------------------------------------------


def histogram_doc(
    name: str, unit: str, source: str, series: List[Series]
) -> Dict[str, Any]:
    return {
        "schema_version": layout.SCHEMA_VERSION,
        "kind": "histogram",
        "name": name,
        "unit": unit,
        "source": source,
        "series": [s.to_dict() for s in series],
    }


def table_doc(
    name: str,
    source: str,
    columns: List[Dict[str, Any]],
    rows: List[List[Any]],
    *,
    limit: Optional[int] = None,
    sort_by: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a table document, optionally keeping only the top N rows.

    Truncation is recorded rather than silent: a table that says it holds
    everything when it does not would make "no other lock matters" a wrong
    conclusion instead of an unknown one.
    """
    total = len(rows)
    if sort_by is not None:
        rows = sorted(rows, key=lambda row: _sort_key(row[sort_by]), reverse=True)
    truncated = limit is not None and total > limit
    if truncated:
        rows = rows[:limit]
    doc: Dict[str, Any] = {
        "schema_version": layout.SCHEMA_VERSION,
        "kind": "table",
        "name": name,
        "source": source,
        "columns": columns,
        "rows": rows,
        "total_rows": total,
    }
    if truncated:
        doc["truncated"] = True
    return doc


def _sort_key(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return float("-inf")
