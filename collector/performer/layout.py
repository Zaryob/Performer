"""Canonical bundle layout.

Every path a bundle may contain is named here once.  The builder writes these
paths, the reader looks them up, the viewer mirrors them.  Nothing else in the
codebase should hard code a bundle relative path string.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

MANIFEST = "manifest.json"

DIR_META = "meta"
DIR_STACKS = "stacks"
DIR_HIST = "hist"
DIR_SERIES = "series"
DIR_GRAPH = "graph"
DIR_RAW = "raw"
DIR_PMU = "pmu"

BUNDLE_DIRS: Tuple[str, ...] = (
    DIR_META,
    DIR_STACKS,
    DIR_HIST,
    DIR_SERIES,
    DIR_GRAPH,
    DIR_RAW,
    DIR_PMU,
)

# meta/
META_SYSTEM = f"{DIR_META}/system.json"
META_TARGET = f"{DIR_META}/target.json"
META_THREADS = f"{DIR_META}/threads.json"

# stacks/ -- FlameGraph "folded" format: "frame;frame;frame <value>"
STACK_ONCPU = f"{DIR_STACKS}/oncpu.folded"
STACK_OFFCPU = f"{DIR_STACKS}/offcpu.folded"
STACK_FUTEX = f"{DIR_STACKS}/futex.folded"
STACK_OFFWAKE = f"{DIR_STACKS}/offwake.folded"

# hist/ -- normalised histograms and aggregation tables
HIST_RUNQLAT = f"{DIR_HIST}/runqlat.json"
HIST_SYSCALL_LATENCY = f"{DIR_HIST}/syscall_latency.json"
HIST_FUTEX_BY_ADDR = f"{DIR_HIST}/futex_by_addr.json"
#: Lock address *and* the call path that waited on it, in one table.  The two
#: separate aggregations cannot be joined after the fact, and a hex address on
#: its own is not something anybody can act on.
HIST_FUTEX_SITES = f"{DIR_HIST}/futex_sites.json"
HIST_FUTEX_DURATION = f"{DIR_HIST}/futex_duration.json"
HIST_OFFCPU_DURATION = f"{DIR_HIST}/offcpu_duration.json"
HIST_OFFCPU_BY_STATE = f"{DIR_HIST}/offcpu_by_state.json"
HIST_THREADLIFE = f"{DIR_HIST}/threadlife.json"
HIST_THREAD_LIFETIME = f"{DIR_HIST}/thread_lifetime.json"
HIST_TIMERS = f"{DIR_HIST}/timers.json"

# series/ -- 1 Hz sampled time series
SERIES_THREADS = f"{DIR_SERIES}/threads.csv"
SERIES_SCHEDSTAT = f"{DIR_SERIES}/schedstat.csv"

# graph/
GRAPH_WAKEUP_EDGES = f"{DIR_GRAPH}/wakeup_edges.json"
PMU_COUNTERS = f"{DIR_PMU}/counters.json"

#: Column headers each series file must start with.  A run whose CSV header
#: does not match is rejected: silently mis-parsed columns are worse than a
#: missing file.
SERIES_HEADERS: Dict[str, Tuple[str, ...]] = {
    SERIES_THREADS: ("t_s", "thread_count", "ctxt_voluntary", "ctxt_involuntary"),
    SERIES_SCHEDSTAT: ("t_s", "run_ns", "wait_ns", "timeslices"),
}

#: Bundle relative path -> schema file name in ``schema/``.
SCHEMA_FOR_PATH: Dict[str, str] = {
    MANIFEST: "manifest.schema.json",
    META_SYSTEM: "system.schema.json",
    META_TARGET: "target.schema.json",
    META_THREADS: "threads.schema.json",
    GRAPH_WAKEUP_EDGES: "wakeup_edges.schema.json",
    PMU_COUNTERS: "pmu.schema.json",
}

#: Files under ``hist/`` carry a ``kind`` discriminator instead of having one
#: schema per file name, because new probes add new files.
SCHEMA_FOR_KIND: Dict[str, str] = {
    "histogram": "hist.schema.json",
    "table": "table.schema.json",
}

#: A 315 thread process can produce tens of thousands of wakeup edges; past a
#: few thousand the graph is unreadable anyway, and the count of what was
#: dropped is recorded in the document.
MAX_WAKEUP_EDGES = 5000

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9._-]{1,64}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PROBE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Anything that ends up in a file name or is executed by the daemon goes
#: through this.  A bundle relative path must be relative, slash separated and
#: free of ``..`` -- enforced both when writing and when extracting.
_RELPATH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


def is_safe_relpath(path: str) -> bool:
    if not path or path != path.strip() or len(path) > 255:
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    if not _RELPATH_RE.match(path):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def run_dir_name(run_id: str) -> str:
    """The single top level directory inside the archive."""
    return f"run_{run_id.replace('-', '_', 1)}"


def archive_name(run_id: str) -> str:
    return f"performer-{run_id}.tgz"


def schema_dir() -> Path:
    """Locate ``schema/`` next to the collector.

    Falls back to ``$PERFORMER_SCHEMA_DIR`` so the collector still validates
    when it has been copied to a target machine as a flat directory.
    """
    override = os.environ.get("PERFORMER_SCHEMA_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    # collector/performer/layout.py -> repo root is two levels up.
    candidates: Sequence[Path] = (
        here.parent.parent.parent / "schema",
        here.parent.parent / "schema",
        here.parent / "schema",
    )
    for candidate in candidates:
        if (candidate / "manifest.schema.json").is_file():
            return candidate
    return candidates[0]


def schema_path(name: str) -> Path:
    return schema_dir() / name


def viewer_index() -> Optional[Path]:
    """Locate the built viewer, if this checkout has one.

    The daemon serves it, which is not a convenience: ``file://`` forbids
    ``fetch``, so a viewer opened by double click cannot call an API at all.
    Being served is what makes "collect a new run from the browser" possible,
    and it is the only difference between the two modes.
    """
    override = os.environ.get("PERFORMER_VIEWER_DIST")
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None
    here = Path(__file__).resolve()
    candidates: Sequence[Path] = (
        here.parent.parent.parent / "viewer" / "dist" / "index.html",
        here.parent.parent / "viewer" / "dist" / "index.html",
        here.parent / "viewer" / "index.html",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def schema_for(bundle_relpath: str, kind: Optional[str] = None) -> Optional[str]:
    """Return the schema file name that governs ``bundle_relpath``, if any."""
    if bundle_relpath in SCHEMA_FOR_PATH:
        return SCHEMA_FOR_PATH[bundle_relpath]
    if bundle_relpath.startswith(f"{DIR_HIST}/") and bundle_relpath.endswith(".json"):
        return SCHEMA_FOR_KIND.get(kind or "")
    return None


def stack_paths() -> List[str]:
    return [STACK_ONCPU, STACK_OFFCPU, STACK_FUTEX, STACK_OFFWAKE]
