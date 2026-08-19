"""Environment validation, run before a single byte is collected.

The point of preflight is to fail *before* an hour of someone's afternoon goes
into a measurement that was never going to be readable.  The frame pointer
check is the one that matters most: without frame pointers every stack is
``[unknown]``, and a flame graph drawn over that is not merely useless, it is
misleading, because it looks exactly like a real one.

Checks, in order (spec section 6.1):

  1. bpftrace present and >= 0.14, and every profile probe file readable
  2. privileges -- root, or CAP_BPF + CAP_PERFMON
  3. target pid exists, and how many threads it has
  4. RLIMIT_NOFILE >= threads x cpus x 2, raised in place when possible
  5. kernel.perf_event_paranoid
  6. frame pointer trial: a 2 second oncpu sample, measuring [unknown] frames
  7. per probe smoke test: anything that produces nothing is disabled, loudly
"""

from __future__ import annotations

import re
import resource
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import proc, profiles
from . import parse
from .parse import stacks
from .profiles import Profile, ProbeSpec
from .runner import ATTACH_GRACE_S, ProbeProcess

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

MIN_BPFTRACE = (0, 14)

#: Above this share of unresolved frames the stacks cannot carry an argument.
UNKNOWN_FRAME_LIMIT = 0.30
UNKNOWN_FRAME_WARN = 0.10

#: Seconds of sampling for the frame pointer trial and each smoke test.
TRIAL_S = 2.0

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass
class Check:
    name: str
    status: str
    message: str
    hint: Optional[str] = None
    details: Dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (PASS, WARN, SKIP)

    def to_dict(self) -> Dict[str, object]:
        doc: Dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.hint:
            doc["hint"] = self.hint
        if self.details:
            doc["details"] = self.details
        return doc


@dataclass
class PreflightReport:
    checks: List[Check] = field(default_factory=list)
    bpftrace_version: Optional[str] = None
    bpftrace_path: Optional[str] = None
    thread_count: int = 0
    comm: Optional[str] = None
    start_time_ticks: Optional[int] = None
    frame_pointers_ok: bool = True
    unknown_frame_ratio: float = 0.0
    unknown_frame_samples: int = 0
    total_frame_samples: int = 0
    #: probe name -> whether its smoke test produced output
    smoke: Dict[str, bool] = field(default_factory=dict)
    smoke_warnings: Dict[str, List[str]] = field(default_factory=dict)
    cpu_before: Optional[Dict[str, object]] = None

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def get(self, name: str) -> Optional[Check]:
        for check in self.checks:
            if check.name == name:
                return check
        return None

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ok(self) -> bool:
        if self.failures:
            return False
        attempted = [c for c in self.checks if c.name.startswith("smoke:") and c.status != SKIP]
        return not attempted or bool(self.usable_probes)

    @property
    def usable_probes(self) -> List[str]:
        return [name for name, good in self.smoke.items() if good]

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "bpftrace_version": self.bpftrace_version,
            "thread_count": self.thread_count,
            "frame_pointers_ok": self.frame_pointers_ok,
            "unknown_frame_ratio": self.unknown_frame_ratio,
            "smoke": dict(self.smoke),
        }


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------


def check_privileges() -> Check:
    ok, caps = proc.have_capabilities()
    if ok:
        how = "root" if caps.get("root") else "CAP_BPF + CAP_PERFMON"
        return Check("privileges", PASS, f"sufficient privileges ({how})", details=caps)
    return Check(
        "privileges",
        FAIL,
        "not root and missing CAP_BPF/CAP_PERFMON; eBPF programs cannot be loaded",
        hint="Run as root, or grant CAP_BPF and CAP_PERFMON to the collector.",
        details=caps,
    )


def check_bpftrace(
    report: PreflightReport,
    *,
    binary: Optional[str] = None,
    runner: Callable[[Sequence[str]], Tuple[int, str, str]] = None,
) -> Check:
    requested = binary or "bpftrace"
    path = shutil.which(requested)
    if path is None:
        return Check(
            "bpftrace",
            FAIL,
            f"bpftrace executable {requested!r} is missing or not executable",
            hint="Install bpftrace >= 0.14, or pass --bpftrace with an executable path.",
        )
    report.bpftrace_path = path
    run = runner or _run_command
    code, out, err = run([path, "--version"])
    text = (out or "") + (err or "")
    match = _VERSION_RE.search(text)
    if code != 0 or match is None:
        return Check(
            "bpftrace",
            FAIL,
            f"'{path} --version' did not report a usable version: {text.strip()[:120]}",
        )
    version = (int(match.group(1)), int(match.group(2)))
    report.bpftrace_version = match.group(0).lstrip("v")
    if version < MIN_BPFTRACE:
        return Check(
            "bpftrace",
            FAIL,
            f"bpftrace {report.bpftrace_version} is older than the required "
            f"{MIN_BPFTRACE[0]}.{MIN_BPFTRACE[1]}",
            details={"path": path},
        )
    return Check(
        "bpftrace",
        PASS,
        f"bpftrace {report.bpftrace_version} at {path}",
        details={"path": path, "version": report.bpftrace_version},
    )


def check_probe_programs(profile: Profile) -> Check:
    """Check every file before running any of the profile's programs."""
    directory = profiles.probes_dir()
    missing = []
    programs = {profiles.ONCPU.program, *(spec.program for spec in profile.probes)}
    for program in sorted(programs):
        path = directory / program
        try:
            readable = path.is_file() and bool(path.stat().st_mode & 0o444)
        except OSError:
            readable = False
        if not readable:
            missing.append(str(path))
    if missing:
        return Check(
            "probe_programs",
            FAIL,
            f"{len(missing)} required probe program(s) missing or unreadable",
            hint="Install the probes/ directory or fix PERFORMER_PROBES_DIR.",
            details={"missing": missing},
        )
    return Check("probe_programs", PASS, f"all {len(programs)} required probe programs are available")


def check_target(report: PreflightReport, pid: int) -> Check:
    if not proc.exists(pid):
        return Check(
            "target",
            FAIL,
            f"no process with pid {pid}",
            hint="Check the pid with 'ps -eLf | grep <name>'.",
        )
    stat = proc.read_stat(pid)
    report.thread_count = proc.thread_count(pid)
    report.comm = stat.comm if stat else proc.read_comm(pid)
    report.start_time_ticks = stat.start_time_ticks if stat else None
    return Check(
        "target",
        PASS,
        f"pid {pid} is '{report.comm}' with {report.thread_count} threads",
        details={"pid": pid, "comm": report.comm, "threads": report.thread_count},
    )


def check_nofile(thread_count: int, cpu_count: int) -> Check:
    """eBPF maps and perf buffers are file descriptors, and there are many.

    A per-CPU perf buffer per thread is the worst case; the spec's
    ``threads x cpus x 2`` is a blunt but adequate bound.  Raising the soft
    limit is free when the hard limit allows it.
    """
    needed = max(1024, thread_count * cpu_count * 2)
    soft, hard = proc.nofile_limit()
    if soft >= needed:
        return Check(
            "nofile",
            PASS,
            f"RLIMIT_NOFILE soft limit {soft} covers the estimated need ({needed})",
            details={"soft": soft, "hard": hard, "needed": needed},
        )
    target = needed if hard == resource.RLIM_INFINITY else min(needed, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        new_soft, _ = proc.nofile_limit()
    except (ValueError, OSError) as exc:
        return Check(
            "nofile",
            WARN,
            f"RLIMIT_NOFILE is {soft}, below the estimated need of {needed}, and "
            f"could not be raised ({exc})",
            hint="Raise it with 'ulimit -n' before collecting, or expect lost events.",
            details={"soft": soft, "hard": hard, "needed": needed},
        )
    if new_soft >= needed:
        return Check(
            "nofile",
            PASS,
            f"raised RLIMIT_NOFILE from {soft} to {new_soft} (need {needed})",
            details={"soft": new_soft, "hard": hard, "needed": needed},
        )
    return Check(
        "nofile",
        WARN,
        f"RLIMIT_NOFILE raised to {new_soft} but the estimated need is {needed}; "
        "the hard limit is in the way",
        hint="Raise the hard limit (LimitNOFILE= in the unit file) to avoid lost events.",
        details={"soft": new_soft, "hard": hard, "needed": needed},
    )


def check_perf_event_paranoid() -> Check:
    value = proc._sysctl_int("kernel.perf_event_paranoid")
    if value is None:
        return Check(
            "perf_event_paranoid",
            WARN,
            "kernel.perf_event_paranoid could not be read",
        )
    is_root = proc.have_capabilities()[1].get("root", False)
    if is_root or value <= 1:
        return Check(
            "perf_event_paranoid",
            PASS,
            f"kernel.perf_event_paranoid = {value}",
            details={"value": value},
        )
    return Check(
        "perf_event_paranoid",
        WARN,
        f"kernel.perf_event_paranoid = {value}; unprivileged profiling is restricted",
        hint="sysctl -w kernel.perf_event_paranoid=1, or run as root.",
        details={"value": value},
    )


# --------------------------------------------------------------------------
# trial runs
# --------------------------------------------------------------------------


@dataclass
class TrialResult:
    ran: bool
    stdout: str
    stderr: str
    exit_reason: str
    folded: List[Tuple[str, int]] = field(default_factory=list)
    stats: stacks.FoldStats = field(default_factory=stacks.FoldStats)
    warnings: List[str] = field(default_factory=list)
    #: Map entries bpftrace actually printed.  Not the same as "stdout is
    #: non-empty": bpftrace always announces "Attaching N probes...", so a
    #: probe that attached and then collected nothing still writes a line.
    map_entries: int = 0

    @property
    def produced_data(self) -> bool:
        return self.ran and self.map_entries > 0


def run_trial(
    spec: ProbeSpec,
    pid: int,
    *,
    bpftrace: str = "bpftrace",
    seconds: float = TRIAL_S,
    attach_grace_s: Optional[float] = None,
    workdir: Optional[Path] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TrialResult:
    """Run one probe briefly and return what it produced.

    Used both for the frame pointer measurement and for the smoke tests --
    the same code path as a real collection, so a probe that works here works
    there.
    """
    temporary = workdir is None
    directory = Path(workdir or tempfile.mkdtemp(prefix="performer-preflight-"))
    directory.mkdir(parents=True, exist_ok=True)
    program = profiles.program_path(spec)
    watchdog = max(1, int(seconds) + 2)
    probe = ProbeProcess(
        name=spec.name,
        argv=[bpftrace, str(program), *spec.probe_args(pid, watchdog)],
        stdout_path=directory / f"{spec.name}.trial.out",
        stderr_path=directory / f"{spec.name}.trial.err",
        env=bpftrace_env(),
    )
    grace = attach_grace_s if attach_grace_s is not None else ATTACH_GRACE_S
    probe.start()
    if not probe.started or not probe.wait_for_attach(grace, sleep=sleep):
        result = TrialResult(
            ran=False,
            stdout=probe.read_stdout(),
            stderr=probe.read_stderr(),
            exit_reason=probe.exit_info.reason,
            warnings=list(probe.warnings),
        )
        _cleanup(directory, temporary)
        return result

    sleep(max(0.0, seconds))
    info = probe.stop(sigint_timeout=10.0, sigterm_timeout=3.0)
    stdout = probe.read_stdout()
    folded, stats, warnings = stacks.parse_oncpu(stdout)
    entries = parse.count_map_entries(stdout)
    result = TrialResult(
        ran=True,
        stdout=stdout,
        stderr=probe.read_stderr(),
        exit_reason=info.reason,
        folded=folded,
        stats=stats,
        warnings=warnings + list(probe.warnings),
        map_entries=entries,
    )
    _cleanup(directory, temporary)
    return result


def _cleanup(directory: Path, temporary: bool) -> None:
    if not temporary:
        return
    try:
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    except OSError:  # pragma: no cover
        pass


def check_frame_pointers(report: PreflightReport, pid: int, **kwargs) -> Check:
    """Two seconds of sampling, then count the frames nobody could name."""
    trial = run_trial(profiles.ONCPU, pid, bpftrace=report.bpftrace_path or "bpftrace", **kwargs)
    if not trial.ran:
        return Check(
            "frame_pointers",
            WARN,
            "the trial oncpu probe did not start, so stack quality is unknown",
            hint="Stack quality could not be measured; see the probe error below.",
            details={"exit_reason": trial.exit_reason, "stderr": trial.stderr[-400:]},
        )
    if trial.stats.total_samples == 0:
        return Check(
            "frame_pointers",
            WARN,
            "the trial produced no samples; the target may be completely idle",
            hint="Measure while the workload is actually running.",
        )

    ratio = trial.stats.unknown_ratio
    report.unknown_frame_ratio = round(ratio, 4)
    report.unknown_frame_samples = trial.stats.unknown_frames
    report.total_frame_samples = trial.stats.total_frames
    report.frame_pointers_ok = ratio <= UNKNOWN_FRAME_LIMIT
    details = {
        "unknown_frame_ratio": report.unknown_frame_ratio,
        "unknown_frames": trial.stats.unknown_frames,
        "total_frames": trial.stats.total_frames,
        "samples": trial.stats.total_samples,
    }
    if ratio > UNKNOWN_FRAME_LIMIT:
        return Check(
            "frame_pointers",
            FAIL,
            f"{ratio:.0%} of sampled frames are [unknown]: the target was built "
            "without frame pointers, so the stacks are unusable",
            hint=(
                "Rebuild the target with -fno-omit-frame-pointer. "
                "Pass --ignore-quality to collect anyway."
            ),
            details=details,
        )
    if ratio > UNKNOWN_FRAME_WARN:
        return Check(
            "frame_pointers",
            WARN,
            f"{ratio:.0%} of sampled frames are [unknown]",
            hint="Some call paths will be attributed to the wrong caller.",
            details=details,
        )
    return Check(
        "frame_pointers",
        PASS,
        f"stacks resolve ({ratio:.1%} unknown frames over "
        f"{trial.stats.total_samples} samples)",
        details=details,
    )


def check_smoke(report: PreflightReport, profile: Profile, pid: int, **kwargs) -> List[Check]:
    """Run every probe briefly; anything silent is disabled, never skipped quietly."""
    checks: List[Check] = []
    for spec in profile.probes:
        trial = run_trial(spec, pid, bpftrace=report.bpftrace_path or "bpftrace", **kwargs)
        produced = trial.produced_data
        report.smoke[spec.name] = produced
        report.smoke_warnings[spec.name] = trial.warnings
        if produced:
            checks.append(
                Check(
                    f"smoke:{spec.name}",
                    PASS,
                    f"probe '{spec.name}' attached and produced output",
                )
            )
            continue
        reason = (
            "did not start"
            if not trial.ran
            else "attached but printed no map data"
        )
        checks.append(
            Check(
                f"smoke:{spec.name}",
                WARN,
                f"probe '{spec.name}' {reason}; it will be recorded as failed",
                hint="Collection can use the other probes if any passed.",
                details={"exit_reason": trial.exit_reason, "stderr": trial.stderr[-400:]},
            )
        )
    return checks


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def bpftrace_env() -> Dict[str, str]:
    """Environment for probe processes.

    A full map silently truncates the data, so the key limits are raised well
    above the defaults: 315 threads x several stacks each overflows the stock
    4096 easily.  Both spellings of the map limit are set because bpftrace
    renamed it; the unknown one is ignored harmlessly.
    """
    return {
        "BPFTRACE_MAX_MAP_KEYS": "1048576",
        "BPFTRACE_MAP_KEYS_MAX": "1048576",
        "BPFTRACE_MAX_PROBES": "1024",
        "BPFTRACE_PERF_RB_PAGES": "512",
        "BPFTRACE_STRLEN": "128",
    }


def _run_command(argv: Sequence[str], timeout: float = 10.0) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def run_preflight(
    pid: int,
    profile: Profile,
    *,
    cpu_count: Optional[int] = None,
    skip_trials: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    overhead_window_s: float = 0.0,
    trial_seconds: float = TRIAL_S,
    attach_grace_s: Optional[float] = None,
    bpftrace: Optional[str] = None,
) -> PreflightReport:
    """Run every check, in order, and stop early only when nothing else can run."""
    report = PreflightReport()

    bpftrace_check = report.add(check_bpftrace(report, binary=bpftrace))
    programs_check = report.add(check_probe_programs(profile))
    if bpftrace_check.status == FAIL or programs_check.status == FAIL:
        reason = "required collection tools are unavailable"
        report.add(Check("frame_pointers", SKIP, f"not run: {reason}"))
        for spec in profile.probes:
            report.smoke[spec.name] = False
            report.add(Check(f"smoke:{spec.name}", SKIP, f"not run: {reason}"))
        return report

    report.add(check_privileges())
    target_check = report.add(check_target(report, pid))

    if target_check.status == FAIL:
        # Nothing downstream can mean anything without a target.
        return report

    import os as _os

    cpus = cpu_count or len(_os.sched_getaffinity(0)) or 1
    report.add(check_nofile(report.thread_count, cpus))
    report.add(check_perf_event_paranoid())

    if skip_trials:
        reason = "trials skipped"
        report.add(Check("frame_pointers", SKIP, f"stack quality not measured: {reason}"))
        for spec in profile.probes:
            report.smoke[spec.name] = False
            report.add(Check(f"smoke:{spec.name}", SKIP, f"not run: {reason}"))
    else:
        trial_kwargs = {
            "sleep": sleep,
            "seconds": trial_seconds,
            "attach_grace_s": attach_grace_s,
        }
        report.add(check_frame_pointers(report, pid, **trial_kwargs))
        for check in check_smoke(report, profile, pid, **trial_kwargs):
            report.add(check)

    if overhead_window_s > 0:
        # Sampled last, so it measures the target as it is about to be traced
        # rather than as it was before the trial probes ran.
        report.cpu_before = proc.sample_cpu(pid, overhead_window_s, sleep=sleep)

    return report


def render(report: PreflightReport) -> str:
    mark = {PASS: "[ok]", WARN: "[!]", FAIL: "[X]", SKIP: "[-]"}
    lines = ["preflight"]
    shown_errors = set()
    missing_tools = any(
        check.status == FAIL and check.name in ("bpftrace", "probe_programs")
        for check in report.checks
    )
    for check in report.checks:
        if missing_tools and check.status == SKIP and (
            check.name == "frame_pointers" or check.name.startswith("smoke:")
        ):
            continue
        lines.append(f"  {mark[check.status]} {check.name:<22} {check.message}")
        if check.hint and check.status in (FAIL, WARN):
            lines.append(f"       -> {check.hint}")
        if check.name == "probe_programs":
            for path in check.details.get("missing", []):
                lines.append(f"       missing: {path}")
        if check.status in (FAIL, WARN):
            cause = trial_error(check)
            if cause and cause not in shown_errors:
                lines.append(f"       cause: {cause}")
                shown_errors.add(cause)
    if missing_tools:
        lines.append("  [-] probe trials           not run: required tools are unavailable")
    return "\n".join(lines)


def trial_error(check: Check) -> Optional[str]:
    """One useful line from a temporary trial log, which is deleted afterward."""
    stderr = check.details.get("stderr")
    if not isinstance(stderr, str):
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower() or "denied" in line.lower()]
    if errors:
        return errors[-1][:240]
    non_warnings = [line for line in lines if not line.lower().startswith("warning")]
    return non_warnings[-1][:240] if non_warnings else None
