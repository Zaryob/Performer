"""Optional process-wide PMU counting via perf_event_open.

Each existing thread needs its own event descriptors: opening an event for the
process leader does not include threads that already exist. New threads are
attached while the measurement runs. Raw counts and kernel scheduling times
are retained so a reader can judge multiplexed measurements for itself.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
import resource
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import layout, proc
from .errors import PreflightError

EVENT_GROUPS = (
    ("cycles", "instructions"),
    ("branches", "branch_misses"),
    ("cache_references", "cache_misses"),
)
CONFIG = {
    "cycles": 0,
    "instructions": 1,
    "cache_references": 2,
    "cache_misses": 3,
    "branches": 4,
    "branch_misses": 5,
}
SYSCALL = {"x86_64": 298, "aarch64": 241}
READ_FORMAT = (1 << 0) | (1 << 1) | (1 << 3)  # enabled, running, group
FLAGS = (1 << 0) | (1 << 5)  # disabled, exclude_kernel
IOC_ENABLE = 0x2400
IOC_DISABLE = 0x2401
IOC_RESET = 0x2403
IOC_GROUP = 1


def _open_event(tid: int, config: int, group_fd: int = -1) -> int:
    machine = platform.machine()
    if platform.system() != "Linux" or machine not in SYSCALL:
        raise PreflightError(f"PMU counting needs Linux x86_64 or aarch64 (found {machine})")
    # PERF_ATTR_SIZE_VER0 (64 bytes). All fields beyond config/read_format and
    # flags are zero. A small known ABI size works on both supported releases.
    attr = ctypes.create_string_buffer(64)
    struct.pack_into("=IIQQQQQIIQ", attr, 0, 0, 64, config, 0, 0, READ_FORMAT, FLAGS, 0, 0, 0)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    fd = libc.syscall(
        ctypes.c_long(SYSCALL[machine]), ctypes.byref(attr), ctypes.c_int(tid),
        ctypes.c_int(-1), ctypes.c_int(group_fd), ctypes.c_ulong(0),
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(fd)


@dataclass
class ThreadCounters:
    tid: int
    start_time_ticks: int
    name: str
    groups: Dict[Tuple[str, str], List[int]] = field(default_factory=dict)
    samples: Dict[Tuple[str, str], Tuple[int, int, int, int]] = field(default_factory=dict)
    enabled_at: Optional[float] = None
    finished_at: Optional[float] = None

    def close(self) -> None:
        for fds in self.groups.values():
            for fd in fds:
                os.close(fd)
        self.groups.clear()


class Session:
    """A single basic PMU measurement; use close() even after failure."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.groups: List[Tuple[str, str]] = []
        self.threads: Dict[Tuple[int, int], ThreadCounters] = {}
        self._active: Set[Tuple[int, int]] = set()
        self.warnings: List[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None
        self._attach_failures = 0
        self._failed_identities: Set[Tuple[int, int]] = set()
        self._initial_count = 0

    def prepare(self) -> None:
        """Check support and attach disabled counters before any run starts."""
        tids = proc.thread_ids(self.pid)
        if not tids:
            raise PreflightError("PMU: target has no readable threads")
        for group in EVENT_GROUPS:
            try:
                fds = self._open_group(tids[0], group)
            except OSError as error:
                if group == EVENT_GROUPS[0]:
                    hint = (
                        "Check perf_event_paranoid and CAP_PERFMON."
                        if error.errno in (errno.EPERM, errno.EACCES)
                        else "The kernel or virtual machine may not expose this PMU event."
                    )
                    raise PreflightError(
                        f"PMU: cycles/instructions unavailable: {error}. "
                        + hint
                    ) from error
                self.warnings.append(f"{group[0]}/{group[1]} unavailable: {error}")
                continue
            for fd in fds:
                os.close(fd)
            self.groups.append(group)
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        needed = len(tids) * len(self.groups) * 2 + 128
        if soft != resource.RLIM_INFINITY and soft < needed:
            raise PreflightError(f"PMU needs about {needed} file descriptors; limit is {soft}")
        for tid in tids:
            self._attach(tid)
        if not self.threads:
            detail = f": {self.warnings[0]}" if self.warnings else ""
            raise PreflightError("PMU: no target thread could be attached" + detail)
        self._initial_count = len(self.threads)

    @staticmethod
    def _open_group(tid: int, group: Tuple[str, str]) -> List[int]:
        fds: List[int] = []
        try:
            leader = _open_event(tid, CONFIG[group[0]])
            fds.append(leader)
            fds.append(_open_event(tid, CONFIG[group[1]], leader))
            return fds
        except BaseException:
            for fd in fds:
                os.close(fd)
            raise

    def _attach(self, tid: int, stat: Optional[proc.Stat] = None) -> None:
        if stat is None:
            stat = proc.read_stat(self.pid, tid)
        if stat is None:
            return
        key = (tid, stat.start_time_ticks)
        if key in self.threads or key in self._failed_identities:
            return
        record = ThreadCounters(tid, stat.start_time_ticks, proc.read_comm(self.pid, tid) or stat.comm)
        try:
            for group in self.groups:
                record.groups[group] = self._open_group(tid, group)
            if self._started_at is not None:
                self._enable(record)
        except OSError as error:
            record.close()
            if error.errno != errno.ESRCH:
                self._failed_identities.add(key)
                self._attach_failures += 1
                if self._attach_failures <= 10:
                    self.warnings.append(f"tid {tid}: PMU attachment failed: {error}")
            return
        self.threads[key] = record
        self._active.add(key)

    @staticmethod
    def _enable(record: ThreadCounters) -> None:
        for fds in record.groups.values():
            fcntl.ioctl(fds[0], IOC_RESET, IOC_GROUP)
            fcntl.ioctl(fds[0], IOC_ENABLE, IOC_GROUP)
        record.enabled_at = time.monotonic()

    def start(self) -> None:
        with self._lock:
            self._started_at = time.monotonic()
            for record in self.threads.values():
                self._enable(record)
        self._watcher = threading.Thread(target=self._watch_threads, daemon=True)
        self._watcher.start()

    def _watch_threads(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                live = set(proc.thread_ids(self.pid))
                now = time.monotonic()
                stats = {}
                for tid in live:
                    stat = proc.read_stat(self.pid, tid)
                    if stat is not None:
                        stats[tid] = stat
                for key in tuple(self._active):
                    record = self.threads[key]
                    current = stats.get(record.tid)
                    departed = record.tid not in live or (
                        current is not None and current.start_time_ticks != record.start_time_ticks
                    )
                    if departed and record.finished_at is None:
                        record.finished_at = now
                        self._capture(record, disable=False)
                for tid, stat in stats.items():
                    self._attach(tid, stat)

    @staticmethod
    def _read_group(fds: List[int]) -> Tuple[int, int, int, int]:
        data = os.read(fds[0], 40)
        if len(data) != 40:
            raise OSError("short PMU group read")
        nr, enabled, running, first, second = struct.unpack("=QQQQQ", data)
        if nr != 2:
            raise OSError(f"unexpected PMU group size {nr}")
        return enabled, running, first, second

    def _capture(self, record: ThreadCounters, *, disable: bool) -> None:
        for group, fds in record.groups.items():
            if disable:
                try:
                    fcntl.ioctl(fds[0], IOC_DISABLE, IOC_GROUP)
                except OSError as error:
                    self.warnings.append(f"tid {record.tid}: PMU stop failed: {error}")
            try:
                record.samples[group] = self._read_group(fds)
            except OSError as error:
                self.warnings.append(f"tid {record.tid}: PMU read failed: {error}")
        record.close()
        self._active.discard((record.tid, record.start_time_ticks))

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=2)
        with self._lock:
            self._ended_at = time.monotonic()
            for key in tuple(self._active):
                record = self.threads[key]
                record.finished_at = self._ended_at
                self._capture(record, disable=True)

    def document(self) -> dict:
        if self._started_at is None or self._ended_at is None:
            raise RuntimeError("PMU session has not finished")
        totals: Dict[str, dict] = {}
        rows: List[dict] = []
        poor_scheduling = False
        for record in sorted(self.threads.values(), key=lambda item: (item.tid, item.start_time_ticks)):
            events: Dict[str, dict] = {}
            groups = dict(record.samples)
            for group, fds in record.groups.items():
                try:
                    groups[group] = self._read_group(fds)
                except OSError as error:
                    self.warnings.append(f"tid {record.tid}: PMU read failed: {error}")
            for group, (enabled, running, first, second) in groups.items():
                if not running or not enabled or running < enabled * 0.9:
                    poor_scheduling = True
                for name, raw in zip(group, (first, second)):
                    scaled = round(raw * enabled / running, 2) if running else None
                    events[name] = {
                        "raw": raw,
                        "scaled": scaled,
                        "time_enabled_ns": enabled,
                        "time_running_ns": running,
                    }
                    total = totals.setdefault(name, {"raw": 0, "scaled": 0.0, "time_enabled_ns": 0, "time_running_ns": 0})
                    total["raw"] += raw
                    if scaled is None:
                        total["scaled"] = None
                    elif total["scaled"] is not None:
                        total["scaled"] += scaled
                    total["time_enabled_ns"] += enabled
                    total["time_running_ns"] += running
            if events:
                rows.append({
                    "tid": record.tid,
                    "name": record.name,
                    "start_time_ticks": record.start_time_ticks,
                    "coverage_s": round(max(0.0, (record.finished_at or self._ended_at) - (record.enabled_at or self._ended_at)), 3),
                    "events": events,
                })
        if poor_scheduling:
            self.warnings.append("some PMU counters ran for less than 90% of enabled time")
        if len(self.groups) < len(EVENT_GROUPS):
            self.warnings.append("some basic PMU event groups are unavailable")
        if self._attach_failures > 10:
            self.warnings.append(f"{self._attach_failures - 10} additional PMU thread attachments failed")
        return {
            "schema_version": layout.SCHEMA_VERSION,
            "kind": "pmu",
            "mode": "basic",
            "source": "perf_event_open",
            "scope": "user",
            "status": "failed" if not rows else "partial" if self.warnings else "ok",
            "cpu_model": proc.system_info().get("cpu_model"),
            "arch": platform.machine(),
            "window_s": round(self._ended_at - self._started_at, 3),
            "thread_count_start": self._initial_count,
            "threads_measured": len(rows),
            "events": list(name for group in self.groups for name in group),
            "totals": totals,
            "threads": rows,
            "warnings": list(dict.fromkeys(self.warnings)),
        }

    def close(self) -> None:
        self._stop.set()
        if self._watcher is not None and self._watcher.is_alive():
            self._watcher.join(timeout=2)
        for record in self.threads.values():
            record.close()
