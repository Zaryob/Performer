"""Supervising probe processes.

The shell script this project replaces lost data in three ways, and all three
are designed out here rather than commented around:

  * **bpftrace writes its maps exactly once, on SIGINT.**  A probe that is
    killed any other way produced nothing, however long it ran.  So the stop
    sequence is SIGINT, wait, SIGTERM, wait, SIGKILL -- and every escalation is
    recorded in the manifest, because it means missing data.
  * **Signals must reach the whole process group.**  Probes are started with
    ``start_new_session=True`` and signalled with :func:`os.killpg`, so a
    bpftrace that re-execs or spawns a helper still gets the signal.
  * **stderr is never discarded.**  Each probe's stderr goes to its own file in
    ``raw/``; it is the only place an attach failure or a lost event count is
    reported.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import proc

#: Deadline for the first eBPF readiness tick. Compilation and loading can
#: take several seconds on a busy machine; process survival is not readiness.
ATTACH_GRACE_S = 60.0
READY_MARKER = "PERFORMER_READY"
_READY_RE = re.compile(r"^PERFORMER_READY\r?$", re.M)

#: bpftrace can take a while to walk and print large maps on SIGINT; with a
#: 315 thread process the stack maps are big and every user stack has to be
#: symbolised, so this is generous on purpose.  Escalating costs every map the
#: probe holds; waiting longer costs only time.
SIGINT_TIMEOUT_S = 90.0

#: How often :func:`stop_all` reports probes that are still writing.
STOP_PROGRESS_EVERY_S = 10.0
SIGTERM_TIMEOUT_S = 5.0

_LOST_EVENTS_RE = re.compile(r"[Ll]ost\s+(\d+)\s+events?")
_MAP_FULL_RE = re.compile(r"Map full|can't update element|Wrote \d+ .*truncat", re.I)
_ERROR_RE = re.compile(r"^(ERROR|error:|stdin:\d+)", re.M)


@dataclass
class ExitInfo:
    reason: str
    exit_code: Optional[int]
    duration_s: float


@dataclass
class ProbeProcess:
    """One bpftrace program, its output files and its exit story."""

    name: str
    argv: Sequence[str]
    stdout_path: Path
    stderr_path: Path
    env: Optional[Dict[str, str]] = None
    warnings: List[str] = field(default_factory=list)

    _proc: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _stdout: Optional[object] = field(default=None, init=False, repr=False)
    _stderr: Optional[object] = field(default=None, init=False, repr=False)
    _started_at: float = field(default=0.0, init=False, repr=False)
    _exit: Optional[ExitInfo] = field(default=None, init=False, repr=False)
    _sigint_sent: bool = field(default=False, init=False, repr=False)
    _sigint_at: float = field(default=0.0, init=False, repr=False)
    _ready_at: Optional[float] = field(default=None, init=False, repr=False)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stdout = self.stdout_path.open("wb")
        self._stderr = self.stderr_path.open("wb")
        environment = dict(os.environ)
        environment.update(self.env or {})
        self._started_at = time.monotonic()
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv is built, never shell
                list(self.argv),
                stdout=self._stdout,
                stderr=self._stderr,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
                shell=False,
            )
        except OSError as exc:
            self._close_files()
            self._exit = ExitInfo("startup_error", None, 0.0)
            self.warnings.append(f"could not start: {exc}")
            self.stderr_path.write_text(
                f"performer: failed to execute {' '.join(self.argv)}: {exc}\n",
                encoding="utf-8",
            )

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def attached(self) -> bool:
        """An executing eBPF interval, not the early 'Attaching' banner."""
        if self._ready_at is None and _READY_RE.search(self.read_stdout()):
            self._ready_at = time.monotonic()
        return self._ready_at is not None

    def abort_startup(self, timeout_s: float) -> None:
        """Stop an alive process that never proved its probes were active."""
        self.warnings.append(
            f"attachment timed out after {timeout_s:g}s without {READY_MARKER}; "
            f"see {self.stderr_path.name}"
        )
        info = self.stop(sigint_timeout=10.0, sigterm_timeout=3.0)
        self._exit = ExitInfo("startup_error", info.exit_code, info.duration_s)

    def check_startup(self) -> bool:
        """True while the probe is still alive; records the failure if not.

        A bpftrace that cannot attach exits within milliseconds, so polling
        this catches a mistyped tracepoint or a missing kernel feature before
        the run wastes a minute producing nothing.
        """
        if self._proc is None:
            return False
        if self._proc.poll() is None:
            return True
        if self._exit is None:
            self._exit = ExitInfo(
                "startup_error",
                self._proc.returncode,
                time.monotonic() - self._started_at,
            )
            self._close_files()
            self.warnings.append(
                f"exited during startup with code {self._proc.returncode}; "
                f"see {self.stderr_path.name}"
            )
        return False

    def wait_for_attach(self, grace_s: float = ATTACH_GRACE_S, *, sleep=time.sleep) -> bool:
        """Wait for this probe's eBPF readiness tick, up to the deadline."""
        return wait_for_attach_all([self], grace_s, sleep=sleep).get(self.name, False)

    # -- stopping -------------------------------------------------------

    def _signal_group(self, sig: int) -> bool:
        if self._proc is None:
            return False
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            # Already gone, or the group vanished between poll and signal.
            try:
                self._proc.send_signal(sig)
                return True
            except (ProcessLookupError, OSError):
                return False
        except OSError:
            return False

    def _wait(self, timeout: float) -> bool:
        if self._proc is None:
            return True
        try:
            self._proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def signal_stop(self) -> None:
        """Send the SIGINT without waiting for the maps to be written.

        Separate from :meth:`stop` so a set of probes can be ended at the same
        instant and then waited on individually.
        """
        if self._exit is None and self._proc is not None and self._proc.poll() is None:
            self._signal_group(signal.SIGINT)
            self._sigint_sent = True
            self._sigint_at = time.monotonic()

    def stop(
        self,
        *,
        sigint_timeout: float = SIGINT_TIMEOUT_S,
        sigterm_timeout: float = SIGTERM_TIMEOUT_S,
    ) -> ExitInfo:
        """SIGINT, then escalate. Every escalation costs data and is recorded."""
        if self._exit is not None:
            return self._exit
        if self._proc is None:
            self._exit = ExitInfo("not_started", None, 0.0)
            return self._exit

        if self._proc.poll() is not None and not self._sigint_sent:
            # Exited on its own -- the in-probe watchdog, or a crash.
            reason = "exited"
        else:
            if not self._sigint_sent:
                self._signal_group(signal.SIGINT)
                self._sigint_at = time.monotonic()
            if self._wait(sigint_timeout):
                reason = "sigint"
            else:
                waited = time.monotonic() - self._sigint_at
                self.warnings.append(
                    f"still running {waited:.0f}s after SIGINT; escalated to "
                    "SIGTERM, so some maps may be missing"
                )
                self._signal_group(signal.SIGTERM)
                if self._wait(sigterm_timeout):
                    reason = "sigterm"
                else:
                    self.warnings.append(
                        "did not respond to SIGTERM; SIGKILLed, so bpftrace never "
                        "wrote its maps and this probe produced no data"
                    )
                    self._signal_group(signal.SIGKILL)
                    self._wait(5.0)
                    reason = "sigkill"

        self._exit = ExitInfo(
            reason, self._proc.returncode, time.monotonic() - self._started_at
        )
        self._close_files()
        return self._exit

    def _close_files(self) -> None:
        for handle in (self._stdout, self._stderr):
            try:
                if handle is not None:
                    handle.close()  # type: ignore[union-attr]
            except OSError:  # pragma: no cover
                pass
        self._stdout = self._stderr = None

    # -- results --------------------------------------------------------

    @property
    def exit_info(self) -> ExitInfo:
        return self._exit or ExitInfo("not_started", None, 0.0)

    def read_stdout(self) -> str:
        try:
            return self.stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def read_stderr(self) -> str:
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# --------------------------------------------------------------------------
# starting and stopping a set of probes together
# --------------------------------------------------------------------------


def wait_for_attach_all(
    probes: Sequence[ProbeProcess],
    grace_s: float = ATTACH_GRACE_S,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, bool]:
    """Wait for every probe's readiness tick within one shared deadline.

    Startup is concurrent rather than granting another deadline per probe.
    Faster probes may already be tracing while slower ones load; this barrier
    ensures every surviving probe is active before the requested run begins.

    bpftrace prints its 'Attaching' banner before loading finishes. Every
    shipped probe therefore emits a first-tick marker from an executing
    interval probe. The measurement can begin only after those ticks arrive.
    Return early once every probe is ready or has failed; stop timed-out
    processes so they cannot quietly join the collection later.
    """
    surviving = {probe.name: False for probe in probes}
    pending = [probe for probe in probes if probe.started]
    deadline = time.monotonic() + grace_s
    while pending:
        for probe in probes:
            if surviving.get(probe.name) and not probe.check_startup():
                surviving[probe.name] = False
        waiting = []
        for probe in pending:
            if not probe.check_startup():
                continue
            if probe.attached:
                surviving[probe.name] = True
            else:
                waiting.append(probe)
        pending = waiting
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        sleep(min(0.05, remaining))
    # End every timed-out startup together. Some processes need seconds to
    # settle, and sequential signalling would leave the others collecting
    # during that cleanup despite having missed the same attach deadline.
    for probe in pending:
        probe.signal_stop()
    for probe in pending:
        probe.abort_startup(grace_s)
    # Timeout cleanup or a slow companion's compilation may outlive a ready
    # probe's watchdog. An old readiness tick is not evidence it is alive now.
    for probe in probes:
        if surviving.get(probe.name) and not probe.check_startup():
            surviving[probe.name] = False
    return surviving


def stop_all(
    probes: Sequence[ProbeProcess],
    *,
    sigint_timeout: float = SIGINT_TIMEOUT_S,
    sigterm_timeout: float = SIGTERM_TIMEOUT_S,
    progress: Optional[Callable[[List[str], float], None]] = None,
    progress_every_s: float = STOP_PROGRESS_EVERY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """End every probe at the same instant, then wait for them one by one.

    Stopping them strictly in sequence would leave the last probe tracing for
    as long as the earlier ones take to write their maps -- which, with a large
    stack map, is seconds. Every probe would then cover a different window
    while the manifest recorded one duration for all of them.

    ``progress`` is called every ``progress_every_s`` with the probes still
    writing and the seconds since SIGINT.  A dump that takes a minute looks
    exactly like a hang otherwise, and an operator who presses Ctrl-C again or
    kills the collector loses what the dump would have produced.
    """
    for probe in probes:
        probe.signal_stop()
    if progress is not None:
        started = time.monotonic()
        next_report = started + progress_every_s
        while True:
            waiting = [probe.name for probe in probes if probe.alive]
            now = time.monotonic()
            if not waiting or now - started >= sigint_timeout:
                break
            if now >= next_report:
                progress(waiting, now - started)
                next_report += progress_every_s
            sleep(0.2)
        # The SIGINT window is shared: the probes were signalled together.
        sigint_timeout = max(0.0, sigint_timeout - (time.monotonic() - started))
    for probe in probes:
        probe.stop(sigint_timeout=sigint_timeout, sigterm_timeout=sigterm_timeout)


# --------------------------------------------------------------------------
# stderr interpretation
# --------------------------------------------------------------------------


@dataclass
class StderrSummary:
    events_lost: int = 0
    warnings: List[str] = field(default_factory=list)
    has_errors: bool = False


def scan_stderr(text: str) -> StderrSummary:
    """Pull the things that change how the data must be read out of stderr.

    Lost events mean every total in the run is a lower bound; a full map means
    the tail of the distribution is simply absent.  Neither is visible in the
    data itself, which is why stderr is kept.
    """
    summary = StderrSummary()
    for match in _LOST_EVENTS_RE.finditer(text):
        summary.events_lost += int(match.group(1))
    if summary.events_lost:
        summary.warnings.append(
            f"kernel dropped {summary.events_lost:,} events; totals are a lower bound"
        )
    if _MAP_FULL_RE.search(text):
        summary.warnings.append(
            "a bpftrace map filled up; raise BPFTRACE_MAX_MAP_KEYS or narrow the probe"
        )
    if _ERROR_RE.search(text):
        summary.has_errors = True
    return summary


# --------------------------------------------------------------------------
# watching the target
# --------------------------------------------------------------------------


class TargetWatcher:
    """Polls ``/proc`` so a dead target is noticed within a second.

    Also catches pid recycling: if the pid comes back as a different process,
    the run must stop rather than quietly measure a stranger.
    """

    def __init__(self, pid: int, *, interval_s: float = 0.5) -> None:
        self.pid = pid
        self.interval_s = interval_s
        stat = proc.read_stat(pid)
        self.start_time_ticks = stat.start_time_ticks if stat else None
        self.died = threading.Event()
        self.died_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="target-watcher", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            if not proc.is_same_process(self.pid, self.start_time_ticks):
                self.died_at = time.time()
                self.died.set()
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 4)


class SeriesSampler:
    """1 Hz ``/proc`` sampling: thread count, context switches, schedstat.

    Costs nothing, needs no privileges, and keeps producing usable data when
    every eBPF probe has failed.  Sampling stops when the target dies -- there
    is nothing left to read.
    """

    def __init__(self, pid: int, *, interval_s: float = 1.0) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self.thread_rows: List[List[int]] = []
        self.schedstat_rows: List[List[int]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = 0.0

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._sample()
        self._thread = threading.Thread(
            target=self._loop, name="series-sampler", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            if not self._sample():
                return

    def _sample(self) -> bool:
        if not proc.is_running(self.pid):
            return False
        elapsed = int(round(time.monotonic() - self._started_at))
        count = proc.thread_count(self.pid)
        voluntary, nonvoluntary = proc.aggregate_ctxt_switches(self.pid)
        self.thread_rows.append([elapsed, count, voluntary, nonvoluntary])
        schedstat = proc.aggregate_schedstat(self.pid)
        self.schedstat_rows.append(
            [elapsed, schedstat.run_ns, schedstat.wait_ns, schedstat.timeslices]
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 3)


# --------------------------------------------------------------------------
# waiting
# --------------------------------------------------------------------------


@dataclass
class WaitOutcome:
    reason: str  # "duration" | "target_died" | "interrupted" | "probes_exited"
    elapsed_s: float


def wait_for_run(
    *,
    duration_s: Optional[float],
    watcher: Optional[TargetWatcher] = None,
    probes: Sequence[ProbeProcess] = (),
    interrupted: Optional[threading.Event] = None,
    poll_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    started_at: Optional[float] = None,
) -> WaitOutcome:
    """Block until the run should end, and say why.

    ``duration_s`` of None means "until the target exits", which is what
    ``collect --until-exit`` asks for.
    """
    started = monotonic() if started_at is None else started_at
    while True:
        elapsed = monotonic() - started
        if duration_s is not None and elapsed >= duration_s:
            return WaitOutcome("duration", elapsed)
        if watcher is not None and watcher.died.is_set():
            return WaitOutcome("target_died", elapsed)
        if interrupted is not None and interrupted.is_set():
            return WaitOutcome("interrupted", elapsed)
        if probes and all(not p.alive for p in probes if p.started):
            return WaitOutcome("probes_exited", elapsed)
        remaining = duration_s - elapsed if duration_s is not None else poll_s
        sleep(min(poll_s, max(0.0, remaining)))
