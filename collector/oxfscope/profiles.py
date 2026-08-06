"""Collection profiles.

Tracing every context switch and every syscall of a 315 thread process
perturbs it badly enough to invalidate the measurement, so probes are grouped
into overhead tiers (spec section 5).

M1 ships one built-in profile, ``oncpu``.  The ``light``/``standard``/``deep``
tiers are YAML files loaded in M2; naming one today gives an explicit error
rather than a silently different measurement.  The stdlib-only rule means M2
also has to bring its own small YAML reader.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from . import layout
from .errors import OxfscopeError


@dataclass(frozen=True)
class ProbeSpec:
    """One bpftrace program and the arguments it runs with."""

    name: str
    program: str
    #: Effective thresholds, recorded in the manifest because they change what
    #: the numbers mean.
    thresholds: Dict[str, float] = field(default_factory=dict)
    #: Bundle relative paths this probe is expected to produce.
    outputs: Tuple[str, ...] = ()
    #: A probe whose failure should fail the whole run rather than degrade it.
    required: bool = False

    def probe_args(self, pid: int, watchdog_s: int) -> Tuple[str, ...]:
        """Positional parameters passed to the .bt program: $1, $2, ..."""
        args = [str(pid), str(watchdog_s)]
        for value in self.thresholds.values():
            args.append(str(int(value)))
        return tuple(args)


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    probes: Tuple[ProbeSpec, ...]
    max_duration_s: int
    expected_overhead: str

    def probe(self, name: str) -> Optional[ProbeSpec]:
        for spec in self.probes:
            if spec.name == name:
                return spec
        return None


ONCPU = ProbeSpec(
    name="oncpu",
    program="oncpu.bt",
    outputs=(layout.STACK_ONCPU,),
    required=True,
)

BUILTIN: Dict[str, Profile] = {
    "oncpu": Profile(
        name="oncpu",
        description=(
            "On-CPU sampling at 99 Hz plus the free /proc series. "
            "The lightest useful measurement: where is the CPU time going."
        ),
        probes=(ONCPU,),
        max_duration_s=600,
        expected_overhead="< 3%",
    ),
}

#: Named in the specification, implemented in M2.  Rejecting them by name is
#: better than quietly running something else under a familiar label.
RESERVED: Dict[str, str] = {
    "light": "M2 -- oncpu, runqlat, threadlife plus /proc series",
    "standard": "M2 -- light plus offcpu, futex and wakeup",
    "deep": "M2 -- standard plus syscall_lat, timers and uprobes",
}

DEFAULT_PROFILE = "oncpu"


def probes_dir() -> Path:
    """Locate ``probes/``, mirroring how the schemas are found."""
    override = os.environ.get("OXFSCOPE_PROBES_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    candidates: Sequence[Path] = (
        here.parent.parent.parent / "probes",
        here.parent.parent / "probes",
        here.parent / "probes",
    )
    for candidate in candidates:
        if (candidate / "oncpu.bt").is_file():
            return candidate
    return candidates[0]


def program_path(spec: ProbeSpec) -> Path:
    """Resolve a probe's ``.bt`` file, refusing anything outside ``probes/``."""
    if "/" in spec.program or spec.program.startswith("."):
        raise OxfscopeError(f"probe program must be a bare file name: {spec.program!r}")
    path = probes_dir() / spec.program
    if not path.is_file():
        raise OxfscopeError(f"probe program not found: {path}")
    return path


def available() -> Dict[str, Profile]:
    return dict(BUILTIN)


def load(name: str) -> Profile:
    if not layout.PROFILE_NAME_RE.match(name):
        raise OxfscopeError(
            f"invalid profile name {name!r}: lowercase letters, digits, dash "
            "and underscore only"
        )
    profile = BUILTIN.get(name)
    if profile is not None:
        return profile
    if name in RESERVED:
        raise OxfscopeError(
            f"profile {name!r} is not implemented yet ({RESERVED[name]}). "
            f"Available now: {', '.join(sorted(BUILTIN))}"
        )
    raise OxfscopeError(
        f"unknown profile {name!r}. Available: {', '.join(sorted(BUILTIN))}"
    )
