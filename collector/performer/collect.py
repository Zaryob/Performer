"""Running a measurement and turning it into a bundle.

The order of operations is the whole design:

    preflight -> baseline CPU sample -> start watcher and sampler ->
    start probes -> wait -> stop probes (SIGINT first) -> snapshot ->
    parse -> write bundle

Nothing is deleted on failure.  A run whose target died after eight seconds
still produces a bundle, marked ``partial``, with the eight seconds in it and
a manifest that says exactly what went wrong.  The alternative -- an empty
directory and a traceback -- is how measurements get repeated at 2am.
"""

from __future__ import annotations

import datetime as _dt
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import emit, layout, manifest as manifest_mod, preflight as preflight_mod
from . import proc, profiles
from . import __version__
from .bundle import BundleBuilder
from .errors import PerformerError, PreflightError
from .profiles import Profile
from .runner import (
    ATTACH_GRACE_S,
    ProbeProcess,
    SeriesSampler,
    TargetWatcher,
    scan_stderr,
    stop_all,
    wait_for_attach_all,
    wait_for_run,
)

Printer = Callable[[str], None]


@dataclass
class CollectOptions:
    pid: int
    label: str
    out_dir: Path
    profile_name: str = profiles.DEFAULT_PROFILE
    duration_s: Optional[float] = 60.0  # None means "until the target exits"
    tags: Sequence[str] = ()
    notes: str = ""
    ignore_quality: bool = False
    force: bool = False
    overhead_window_s: float = 5.0
    keep_raw_stdout: bool = False
    pack: bool = True
    annotate_kernel: bool = False
    bpftrace: Optional[str] = None
    #: Trial durations used by preflight. Shortened by the test suite; there
    #: is no CLI flag because a shorter trial measures stack quality worse.
    preflight_trial_s: float = preflight_mod.TRIAL_S
    preflight_attach_grace_s: Optional[float] = None
    #: How long to watch the probes for an early exit before the run starts.
    #: Shared across all of them, so it does not scale with the probe count.
    attach_grace_s: Optional[float] = None


@dataclass
class CollectResult:
    run_dir: Path
    archive: Optional[Path]
    manifest: Dict[str, Any]
    preflight: preflight_mod.PreflightReport
    probes: List[manifest_mod.ProbeResult] = field(default_factory=list)


def collect(
    options: CollectOptions,
    *,
    printer: Printer = print,
    cancel: Optional[threading.Event] = None,
) -> CollectResult:
    """Measure a process and write a run bundle.

    ``cancel`` is how a caller that is not a terminal stops a run: setting it
    ends the collection exactly as Ctrl-C would, which means the probes are
    still SIGINTed in order and the bundle is still written.  A cancelled run
    is a short run, not a lost one -- there is no path here that throws away
    data that has already been paid for.
    """
    profile = profiles.load(options.profile_name)
    _check_duration(options, profile)

    # ---- preflight ---------------------------------------------------
    printer(f"preflight: pid {options.pid}, profile '{profile.name}'")
    report = preflight_mod.run_preflight(
        options.pid,
        profile,
        overhead_window_s=options.overhead_window_s,
        trial_seconds=options.preflight_trial_s,
        attach_grace_s=options.preflight_attach_grace_s,
    )
    printer(preflight_mod.render(report))

    blocking = _blocking_failures(report, options)
    if blocking:
        raise PreflightError(
            "preflight failed:\n"
            + "\n".join(f"  - {c.name}: {c.message}" for c in blocking)
            + _override_hint(blocking)
        )
    if not report.usable_probes:
        raise PreflightError(
            "no probe survived its smoke test; there is nothing to collect. "
            "See the messages above."
        )

    # ---- set up the bundle ------------------------------------------
    started_at = manifest_mod.utc_now()
    builder = BundleBuilder(options.out_dir, label=options.label, started_at=started_at)
    printer(f"run {builder.run_id}")
    builder.add_json(
        f"{layout.DIR_RAW}/preflight.json", report.to_dict()
    )

    target_static = proc.target_info(options.pid)
    threads_start = proc.snapshot_threads(options.pid)

    watcher = TargetWatcher(options.pid)
    sampler = SeriesSampler(options.pid)
    # The caller's event when there is one, so a cancellation that arrives
    # while the probes are still attaching is not lost between the two.
    interrupted = cancel if cancel is not None else threading.Event()

    launched = _launch_probes(builder, profile, report, options, printer)

    # The CPU reading and the clock have to start at the same instant. Taking
    # the reading before the probes attach would charge the attach time --
    # seconds of it, growing with the number of probes -- to a window measured
    # from after it, inflating the overhead estimate in proportion to how much
    # was being measured.
    ticks_start = proc.cpu_ticks(options.pid)
    run_started = time.monotonic()
    watcher.start()
    sampler.start()

    outcome = None
    with _interrupt_guard(interrupted, printer):
        outcome = wait_for_run(
            duration_s=options.duration_s,
            watcher=watcher,
            probes=[p for _spec, p in launched],
            interrupted=interrupted,
        )
    # Closed together, for the same reason they were opened together.
    elapsed = time.monotonic() - run_started
    ticks_end = proc.cpu_ticks(options.pid)

    # ---- stop everything --------------------------------------------
    printer(f"stopping probes after {elapsed:.1f}s ({outcome.reason})")
    sampler.stop()
    watcher.stop()
    stop_all([probe for _spec, probe in launched])

    threads_end = proc.snapshot_threads(options.pid)
    ended_at = manifest_mod.utc_now()

    cpu_after = (
        proc.sample_cpu(options.pid, options.overhead_window_s)
        if options.overhead_window_s > 0
        else None
    )

    # ---- turn probe output into bundle files -------------------------
    probe_results = _finish_probes(
        builder, launched, report, profile, options, printer, elapsed
    )

    thread_count_start = len(threads_start)
    thread_count_end = len(threads_end)
    builder.add_json(layout.META_SYSTEM, proc.system_info(options.pid))
    target_doc = dict(target_static)
    target_doc.update(
        {
            "thread_count_start": thread_count_start,
            "thread_count_end": thread_count_end,
            "died_during_run": watcher.died.is_set(),
            "cpu_before": report.cpu_before,
            "cpu_after": cpu_after,
        }
    )
    builder.add_json(layout.META_TARGET, target_doc)
    builder.add_json(
        layout.META_THREADS,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "sampled_at_start": manifest_mod.format_ts(started_at),
            "sampled_at_end": manifest_mod.format_ts(ended_at),
            "clk_tck": proc.CLK_TCK,
            "threads": proc.merge_thread_snapshots(threads_start, threads_end),
        },
    )
    if sampler.thread_rows:
        builder.add_series(layout.SERIES_THREADS, sampler.thread_rows)
    if sampler.schedstat_rows:
        builder.add_series(layout.SERIES_SCHEDSTAT, sampler.schedstat_rows)

    # ---- quality ------------------------------------------------------
    during_pct = proc.cpu_pct_between(ticks_start, ticks_end, elapsed)
    baseline_pct = proc.baseline_cpu_pct([report.cpu_before, cpu_after])
    quality = _build_quality(
        report, probe_results, options, baseline_pct, during_pct, cpu_after
    )

    # With --until-exit the target exiting *is* the stop condition, so it is
    # a normal completion rather than a degradation. Recording it as a death
    # would mark every such run "partial" and make the badge meaningless.
    until_exit = options.duration_s is None
    ended_by_target_exit = outcome.reason == "target_died"

    warnings: List[str] = []
    if ended_by_target_exit and until_exit:
        warnings.append("target exited, ending the run as requested by --until-exit")
    elif ended_by_target_exit:
        warnings.append("target process exited before the requested duration elapsed")
    elif outcome.reason == "interrupted":
        warnings.append("collection was interrupted by the operator")
    elif outcome.reason == "probes_exited":
        warnings.append("every probe exited on its own before the duration elapsed")
    if options.ignore_quality and not report.frame_pointers_ok:
        warnings.append(
            "collected with --ignore-quality despite a failed frame pointer check; "
            "the stacks in this bundle are not trustworthy"
        )

    died_at = None
    if watcher.died.is_set() and watcher.died_at is not None and not until_exit:
        died_at = _dt.datetime.fromtimestamp(watcher.died_at, _dt.timezone.utc).replace(
            microsecond=0
        )

    document = manifest_mod.build_manifest(
        label=options.label,
        profile=profile.name,
        # For --until-exit there is no requested duration; reporting the
        # measured one keeps "requested vs actual" from becoming noise.
        duration_s=float(options.duration_s) if not until_exit else round(elapsed, 2),
        started_at=started_at,
        ended_at=ended_at,
        actual_duration_s=round(elapsed, 2),
        target=manifest_mod.TargetInfo(
            pid=options.pid,
            comm=str(target_static.get("comm") or "unknown"),
            thread_count_start=thread_count_start,
            thread_count_end=thread_count_end,
            cmdline=target_static.get("cmdline") or None,
            exe=target_static.get("exe"),
        ),
        probes=probe_results,
        quality=quality,
        tool_versions={
            "performer": __version__,
            "bpftrace": report.bpftrace_version,
            "kernel": proc.system_info().get("kernel"),
            "distro": proc.system_info().get("distro"),
        },
        tags=list(options.tags),
        notes=options.notes,
        target_died_at=died_at,
        warnings=warnings,
    )
    builder.write_manifest(document)

    archive = builder.pack() if options.pack else None
    return CollectResult(
        run_dir=builder.root,
        archive=archive,
        manifest=document,
        preflight=report,
        probes=probe_results,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _check_duration(options: CollectOptions, profile: Profile) -> None:
    if options.duration_s is None:
        return
    if options.duration_s <= 0:
        raise PerformerError("--duration must be greater than zero")
    if options.duration_s > profile.max_duration_s and not options.force:
        raise PerformerError(
            f"profile '{profile.name}' is not meant to run longer than "
            f"{profile.max_duration_s}s (expected overhead {profile.expected_overhead}); "
            f"{options.duration_s:g}s was requested. Re-run with --force to override."
        )


def _blocking_failures(
    report: preflight_mod.PreflightReport, options: CollectOptions
) -> List[preflight_mod.Check]:
    """Which preflight failures actually stop the run.

    ``--ignore-quality`` waives the frame pointer verdict and nothing else:
    the operator can decide that unusable stacks are acceptable, but cannot
    decide that bpftrace is installed.
    """
    blocking = []
    for check in report.failures:
        if check.name == "frame_pointers" and options.ignore_quality:
            continue
        if check.name.startswith("smoke:") and options.force:
            continue
        blocking.append(check)
    return blocking


def _override_hint(blocking: Sequence[preflight_mod.Check]) -> str:
    names = {c.name for c in blocking}
    if "frame_pointers" in names:
        return "\n\nPass --ignore-quality to collect anyway (the stacks will be unusable)."
    if any(n.startswith("smoke:") for n in names):
        return "\n\nPass --force to collect with the failing probes disabled."
    return ""


class _interrupt_guard:
    """Turn the operator's Ctrl-C into a clean stop instead of a traceback.

    The probes live in their own process groups, so a terminal Ctrl-C reaches
    only the collector.  That is deliberate: the collector must be the one to
    SIGINT bpftrace, in the right order, or the maps are never written.
    """

    def __init__(self, event: threading.Event, printer: Printer) -> None:
        self.event = event
        self.printer = printer
        self._previous = None

    def __enter__(self) -> "_interrupt_guard":
        def _handler(_signum, _frame):
            if not self.event.is_set():
                self.printer("interrupted: stopping probes and writing the bundle")
            self.event.set()

        try:
            self._previous = signal.signal(signal.SIGINT, _handler)
        except ValueError:  # pragma: no cover - not the main thread
            self._previous = None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._previous is not None:
            try:
                signal.signal(signal.SIGINT, self._previous)
            except ValueError:  # pragma: no cover
                pass


def _launch_probes(
    builder: BundleBuilder,
    profile: Profile,
    report: preflight_mod.PreflightReport,
    options: CollectOptions,
    printer: Printer,
) -> List[tuple]:
    """Start every probe that passed its smoke test."""
    bpftrace = options.bpftrace or report.bpftrace_path or "bpftrace"
    watchdog = int((options.duration_s or 0) + 30) if options.duration_s else 86400
    # Every probe is started before any of them is waited on, so they all
    # attach at effectively the same moment and the window the manifest
    # records is the window they all covered.
    launched: List[tuple] = []
    for spec in profile.probes:
        if not report.smoke.get(spec.name):
            continue
        probe = ProbeProcess(
            name=spec.name,
            argv=[
                bpftrace,
                str(profiles.program_path(spec)),
                *spec.probe_args(options.pid, watchdog),
            ],
            stdout_path=builder.root / layout.DIR_RAW / f"{spec.name}.stdout.log",
            stderr_path=builder.probe_log_path(spec.name),
            env=preflight_mod.bpftrace_env(),
        )
        probe.start()
        launched.append((spec, probe))

    surviving = wait_for_attach_all(
        [probe for _spec, probe in launched],
        options.attach_grace_s or ATTACH_GRACE_S,
    )
    for spec, probe in launched:
        if surviving.get(spec.name):
            printer(f"  probe '{spec.name}' attached (pid {probe.pid})")
        else:
            printer(
                f"  probe '{spec.name}' failed to start; see raw/{spec.name}.stderr.log"
            )
    return launched


def _finish_probes(
    builder: BundleBuilder,
    launched: Sequence[tuple],
    report: preflight_mod.PreflightReport,
    profile: Profile,
    options: CollectOptions,
    printer: Printer,
    elapsed_s: float,
) -> List[manifest_mod.ProbeResult]:
    """Parse each probe's output into the bundle and judge how it went."""
    results: List[manifest_mod.ProbeResult] = []
    launched_by_name = {spec.name: (spec, probe) for spec, probe in launched}
    context = emit.EmitContext(
        builder=builder,
        duration_s=elapsed_s,
        annotate_kernel=options.annotate_kernel,
    )

    for spec in profile.probes:
        if spec.name not in launched_by_name:
            results.append(
                manifest_mod.ProbeResult(
                    name=spec.name,
                    status="failed",
                    exit_reason="not_started",
                    warnings=[
                        "disabled by preflight: the smoke test produced no output"
                    ],
                    thresholds=dict(spec.thresholds) or None,
                )
            )
            continue

        _spec, probe = launched_by_name[spec.name]
        info = probe.exit_info
        stderr_summary = scan_stderr(probe.read_stderr())
        warnings = list(probe.warnings) + list(stderr_summary.warnings)
        outputs: List[str] = []
        status = "ok"

        stdout = probe.read_stdout()
        emitted = emit.emit(spec.name, context, stdout)
        warnings.extend(emitted.warnings)
        outputs.extend(emitted.outputs)
        for note in emitted.notes:
            printer(f"  {note}")

        # The on-CPU profile is the sample the quality block is computed
        # from, and the real run is a far better estimate of symbolisation
        # quality than the two second preflight trial.
        if emitted.fold_stats is not None and spec.name == "oncpu":
            stats = emitted.fold_stats
            if stats.total_frames:
                report.unknown_frame_ratio = round(stats.unknown_ratio, 4)
                report.unknown_frame_samples = stats.unknown_frames
                report.total_frame_samples = stats.total_frames

        if info.reason in ("sigkill", "startup_error", "not_started"):
            status = "failed"
        elif not outputs:
            status = "failed"
        elif info.reason == "sigterm" or stderr_summary.events_lost:
            status = "partial"
        elif stderr_summary.has_errors:
            status = "partial"

        # bpftrace writes its maps only on SIGINT: a probe killed any other
        # way produced nothing, whatever its exit code says.
        if info.reason == "sigkill" and outputs:  # pragma: no cover - defensive
            warnings.append("output predates the SIGKILL and may be truncated")

        results.append(
            manifest_mod.ProbeResult(
                name=spec.name,
                status=status,
                events_lost=stderr_summary.events_lost,
                warnings=warnings,
                duration_s=round(info.duration_s, 2),
                exit_reason=info.reason,
                exit_code=info.exit_code,
                thresholds=dict(spec.thresholds) or None,
                outputs=outputs,
            )
        )

        if not options.keep_raw_stdout:
            # The folded file carries everything; the raw map dump is large
            # and would double the bundle for no benefit.
            probe.stdout_path.unlink(missing_ok=True)

    return results


def _build_quality(
    report: preflight_mod.PreflightReport,
    probe_results: Sequence[manifest_mod.ProbeResult],
    options: CollectOptions,
    baseline_pct: Optional[float],
    during_pct: Optional[float],
    cpu_after: Optional[Dict[str, Any]] = None,
) -> manifest_mod.Quality:
    notes: List[str] = []
    if options.ignore_quality:
        notes.append("frame pointer check overridden with --ignore-quality")
    if baseline_pct is None or during_pct is None:
        notes.append("overhead could not be estimated: CPU samples were unavailable")
    disagreement = proc.baseline_disagreement([report.cpu_before, cpu_after])
    if disagreement is not None and disagreement > 0.25:
        notes.append(
            f"the target's untraced CPU differed by {disagreement:.0%} between the "
            "samples taken before and after the run; its own load was not steady, "
            "so the overhead estimate is unreliable"
        )

    overhead: Optional[Dict[str, float]] = None
    if baseline_pct is not None or during_pct is not None:
        overhead = {}
        if report.cpu_before and report.cpu_before.get("cpu_pct") is not None:
            overhead["cpu_pct_before"] = float(report.cpu_before["cpu_pct"])
        if during_pct is not None:
            overhead["cpu_pct_during"] = during_pct
        if cpu_after and cpu_after.get("cpu_pct") is not None:
            overhead["cpu_pct_after"] = float(cpu_after["cpu_pct"])
        overhead["sample_window_s"] = options.overhead_window_s

    total = report.total_frame_samples
    unknown = report.unknown_frame_samples
    ratio = round(unknown / total, 4) if total else report.unknown_frame_ratio

    return manifest_mod.Quality(
        frame_pointers_ok=report.frame_pointers_ok,
        unknown_frame_ratio=ratio,
        estimated_overhead_pct=proc.overhead_pct(baseline_pct, during_pct),
        # Emitted together or not at all: the manifest's consistency rule
        # compares the ratio against these two, and a lone total tells nobody
        # anything.
        unknown_frame_samples=unknown if total else None,
        total_frame_samples=total or None,
        overhead=overhead,
        ignore_quality=True if options.ignore_quality else None,
        notes=notes,
    )
