"""Collection profiles.

Tracing every context switch and every syscall of a 315 thread process
perturbs it badly enough to invalidate the measurement, so probes are grouped
into overhead tiers (spec section 5), defined as YAML in ``collector/profiles``
so they can be edited on the target machine without touching code.

Profile names are a whitelist, not paths.  That matters beyond tidiness: from
M6 the name is the only probe-selecting input the daemon accepts from the
network, so it is validated against a pattern here and resolved against a
fixed directory, never joined with anything a caller supplies.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import layout, yamlish
from .errors import PerformerError

DEFAULT_PROFILE = "standard"
DEFAULT_ONCPU_HZ = 99
MIN_ONCPU_HZ = 1
MAX_ONCPU_HZ = 4000
_ONCPU_ATTACHPOINT = re.compile(r"^profile:hz:99[ \t]*$", re.MULTILINE)
_ANY_PROFILE_ATTACHPOINT = re.compile(r"^[ \t]*profile:hz:[^\n]+$", re.MULTILINE)

#: Keys a profile document may contain.  An unknown key is an error, because
#: the likeliest cause is a typo in a threshold, and a silently ignored
#: threshold changes what the numbers mean.
_PROFILE_KEYS = {"name", "description", "expected_overhead", "max_duration_s", "probes"}
_PROBE_KEYS = {"name", "program", "thresholds", "required"}


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
    source: Optional[str] = None

    def probe(self, name: str) -> Optional[ProbeSpec]:
        for spec in self.probes:
            if spec.name == name:
                return spec
        return None

    @property
    def probe_names(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.probes)


ONCPU = ProbeSpec(
    name="oncpu", program="oncpu.bt", outputs=(layout.STACK_ONCPU,), required=True
)


# --------------------------------------------------------------------------
# locating files
# --------------------------------------------------------------------------


def _search_dirs(env_var: str, name: str, marker: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    candidates: Sequence[Path] = (
        here.parent.parent.parent / name,
        here.parent.parent / name,
        here.parent / name,
    )
    for candidate in candidates:
        if (candidate / marker).is_file():
            return candidate
    return candidates[0]


def probes_dir() -> Path:
    return _search_dirs("PERFORMER_PROBES_DIR", "probes", "oncpu.bt")


def profiles_dir() -> Path:
    return _search_dirs("PERFORMER_PROFILES_DIR", "collector/profiles", "standard.yaml")


def program_path(spec: ProbeSpec) -> Path:
    """Resolve a probe's ``.bt`` file, refusing anything outside ``probes/``."""
    if "/" in spec.program or spec.program.startswith("."):
        raise PerformerError(f"probe program must be a bare file name: {spec.program!r}")
    path = probes_dir() / spec.program
    if not path.is_file():
        raise PerformerError(f"probe program not found: {path}")
    return path


def validate_oncpu_hz(value: int) -> int:
    """Keep the generated bpftrace attachpoint a bounded integer literal."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_ONCPU_HZ <= value <= MAX_ONCPU_HZ
    ):
        raise PerformerError(
            f"--oncpu-hz must be an integer from {MIN_ONCPU_HZ} to {MAX_ONCPU_HZ}"
        )
    return value


def probe_command(
    spec: ProbeSpec,
    pid: int,
    watchdog_s: int,
    *,
    bpftrace: str,
    oncpu_hz: int = DEFAULT_ONCPU_HZ,
    generated_dir: Optional[Path] = None,
    window_control_pid: Optional[int] = None,
    window_control_token: int = 0,
) -> List[str]:
    """Build the same probe command for preflight and actual collection.

    Keep the shipped file untouched at the default rate. At another rate,
    write a copy with exactly one on-CPU attachpoint changed; this also keeps
    the positional parameters compatible with older bpftrace releases.
    """
    validate_oncpu_hz(oncpu_hz)
    program = program_path(spec)
    source = program.read_text(encoding="utf-8")
    if spec.name == "oncpu":
        if (
            len(_ANY_PROFILE_ATTACHPOINT.findall(source)) != 1
            or len(_ONCPU_ATTACHPOINT.findall(source)) != 1
        ):
            raise PerformerError(
                f"{program}: expected exactly one profile:hz:{DEFAULT_ONCPU_HZ} "
                "attachpoint to record --oncpu-hz accurately"
            )
        if oncpu_hz != DEFAULT_ONCPU_HZ:
            if generated_dir is None:
                raise PerformerError("a directory is required for a generated oncpu probe")
            source = _ONCPU_ATTACHPOINT.sub(f"profile:hz:{oncpu_hz}", source)
    if window_control_pid is not None:
        from .window import render_program
        source = render_program(
            source, window_control_pid, offcpu=spec.name == "offcpu",
            control_token=window_control_token,
        )
    if window_control_pid is not None or (spec.name == "oncpu" and oncpu_hz != DEFAULT_ONCPU_HZ):
        if generated_dir is None:
            raise PerformerError("a directory is required for a generated probe")
        destination = Path(generated_dir) / spec.program
        if destination.resolve() == program.resolve():
            raise PerformerError("generated probe cannot overwrite the installed program")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        program = destination
    # Readiness comes from printf in the first executing eBPF interval. A
    # file-backed stdout must flush that line before collection can begin.
    return [bpftrace, "-B", "line", str(program), *spec.probe_args(pid, watchdog_s)]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _require(document: Dict[str, Any], key: str, source: str) -> Any:
    if key not in document or document[key] is None:
        raise PerformerError(f"{source}: missing required key '{key}'")
    return document[key]


def _parse_probe(entry: Any, source: str, index: int) -> ProbeSpec:
    where = f"{source}: probes[{index}]"
    if not isinstance(entry, dict):
        raise PerformerError(f"{where} must be a mapping with 'name' and 'program'")
    unknown = set(entry) - _PROBE_KEYS
    if unknown:
        raise PerformerError(
            f"{where}: unknown key(s) {sorted(unknown)}; expected "
            f"{sorted(_PROBE_KEYS)}"
        )
    name = _require(entry, "name", where)
    program = _require(entry, "program", where)
    if not isinstance(name, str) or not layout.PROBE_NAME_RE.match(name):
        raise PerformerError(f"{where}: invalid probe name {name!r}")
    if not isinstance(program, str) or "/" in program or not program.endswith(".bt"):
        raise PerformerError(
            f"{where}: program must be a bare '.bt' file name, got {program!r}"
        )

    thresholds: Dict[str, float] = {}
    raw_thresholds = entry.get("thresholds") or {}
    if not isinstance(raw_thresholds, dict):
        raise PerformerError(f"{where}: thresholds must be a mapping")
    for key, value in raw_thresholds.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PerformerError(
                f"{where}: threshold {key!r} must be a number, got {value!r}"
            )
        thresholds[key] = float(value)

    required = entry.get("required", False)
    if not isinstance(required, bool):
        raise PerformerError(f"{where}: 'required' must be true or false")

    return ProbeSpec(
        name=name, program=program, thresholds=thresholds, required=required
    )


def parse_profile(document: Any, *, source: str) -> Profile:
    if not isinstance(document, dict):
        raise PerformerError(f"{source}: a profile must be a mapping")
    unknown = set(document) - _PROFILE_KEYS
    if unknown:
        raise PerformerError(
            f"{source}: unknown key(s) {sorted(unknown)}; expected "
            f"{sorted(_PROFILE_KEYS)}"
        )

    name = _require(document, "name", source)
    if not isinstance(name, str) or not layout.PROFILE_NAME_RE.match(name):
        raise PerformerError(f"{source}: invalid profile name {name!r}")

    raw_probes = _require(document, "probes", source)
    if not isinstance(raw_probes, list) or not raw_probes:
        raise PerformerError(f"{source}: 'probes' must be a non-empty list")

    probes: List[ProbeSpec] = []
    seen = set()
    for index, entry in enumerate(raw_probes):
        spec = _parse_probe(entry, source, index)
        if spec.name in seen:
            raise PerformerError(f"{source}: probe {spec.name!r} listed twice")
        seen.add(spec.name)
        probes.append(spec)

    max_duration = document.get("max_duration_s", 300)
    if not isinstance(max_duration, int) or isinstance(max_duration, bool) or max_duration <= 0:
        raise PerformerError(
            f"{source}: 'max_duration_s' must be a positive integer, got {max_duration!r}"
        )

    return Profile(
        name=name,
        description=str(document.get("description") or ""),
        probes=tuple(probes),
        max_duration_s=max_duration,
        expected_overhead=str(document.get("expected_overhead") or "unknown"),
        source=source,
    )


def profile_path(name: str) -> Path:
    if not layout.PROFILE_NAME_RE.match(name):
        raise PerformerError(
            f"invalid profile name {name!r}: lowercase letters, digits, dash "
            "and underscore only"
        )
    return profiles_dir() / f"{name}.yaml"


def available() -> Dict[str, Path]:
    """Profile name -> file, for everything installed."""
    directory = profiles_dir()
    if not directory.is_dir():
        return {}
    found: Dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        stem = path.stem
        if layout.PROFILE_NAME_RE.match(stem):
            found[stem] = path
    return found


def load(name: str) -> Profile:
    path = profile_path(name)
    if not path.is_file():
        known = ", ".join(sorted(available())) or "none installed"
        raise PerformerError(f"unknown profile {name!r}. Available: {known}")
    try:
        document = yamlish.load_file(path)
    except yamlish.YamlError as exc:
        raise PerformerError(str(exc)) from None
    profile = parse_profile(document, source=str(path))
    if profile.name != name:
        raise PerformerError(
            f"{path}: profile calls itself {profile.name!r} but the file is "
            f"{name}.yaml; the name in the file is what the manifest records"
        )
    return profile


def load_all() -> List[Profile]:
    return [load(name) for name in sorted(available())]
