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

#: Time to let a probe attach before deciding it failed at startup.  bpftrace
#: needs to compile and load the program, which is not instant on a busy box.
ATTACH_GRACE_S = 2.0

#: bpftrace can take a while to walk and print large maps on SIGINT; with a
#: 315 thread process the stack map is big, so this is generous on purpose.
SIGINT_TIMEOUT_S = 30.0
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
                f"oxfscope: failed to execute {' '.join(self.argv)}: {exc}\n",
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

    def wait_for_attach(self, grace_s: float = ATTACH_GRACE_S, *, sleep=time.sleep) -> bool:
        """Return False when the probe died during startup.

        A bpftrace that cannot attach exits within milliseconds, so this
        catches a mistyped tracepoint or a missing kernel feature before the
        run wastes a minute producing nothing.
        """
        if self._proc is None:
            return False
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
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
            sleep(0.05)
        return True

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

        if self._proc.poll() is not None:
            # Exited on its own -- the in-probe watchdog, or a crash.
            reason = "exited"
        else:
            self._signal_group(signal.SIGINT)
            if self._wait(sigint_timeout):
                reason = "sigint"
            else:
                self.warnings.append(
                    f"still running {sigint_timeout:.0f}s after SIGINT; escalated to "
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
) -> WaitOutcome:
    """Block until the run should end, and say why.

    ``duration_s`` of None means "until the target exits", which is what
    ``collect --until-exit`` asks for.
    """
    started = monotonic()
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
        sleep(poll_s)
