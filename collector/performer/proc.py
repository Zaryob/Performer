"""Readers for ``/proc``.

Everything here is free: no eBPF, no root, no perturbation of the target.  It
is also the only data source that still works when every probe fails, which is
why the thread inventory and the 1 Hz series come from here rather than from a
tracepoint.

All readers tolerate a thread or process disappearing between the moment its
directory is listed and the moment it is read -- with 315 threads coming and
going, that is the normal case, not an edge case.
"""

from __future__ import annotations

import os
import resource
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROC = Path("/proc")

try:
    CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):  # pragma: no cover - exotic platforms
    CLK_TCK = 100


# --------------------------------------------------------------------------
# low level helpers
# --------------------------------------------------------------------------


def _read_text(path: Path) -> Optional[str]:
    """Read a /proc file, returning None if it vanished or is unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def proc_path(pid: int, *parts: str) -> Path:
    return PROC.joinpath(str(pid), *parts)


def task_path(pid: int, tid: int, *parts: str) -> Path:
    return PROC.joinpath(str(pid), "task", str(tid), *parts)


def exists(pid: int) -> bool:
    return proc_path(pid).is_dir()


#: Process states that mean "finished, just not reaped yet".
_DEAD_STATES = frozenset("ZXx")


def is_running(pid: int) -> bool:
    """True when the pid exists *and* is still a live process.

    A killed process whose parent has not reaped it stays in ``/proc`` as a
    zombie: the directory is there, the threads are not.  Treating that as
    alive makes the collector sit out the rest of its duration measuring
    nothing, so the state field is checked rather than just the directory.

    An unreadable ``stat`` counts as gone.  The collector needs root anyway,
    so the realistic cause is that the process disappeared mid-read.
    """
    stat = read_stat(pid)
    return stat is not None and stat.state not in _DEAD_STATES


# --------------------------------------------------------------------------
# process level
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    """The fields of ``/proc/<pid>/stat`` this project uses."""

    pid: int
    comm: str
    state: str
    num_threads: int
    utime_ticks: int
    stime_ticks: int
    start_time_ticks: int


def read_stat(pid: int, tid: Optional[int] = None) -> Optional[Stat]:
    path = proc_path(pid, "stat") if tid is None else task_path(pid, tid, "stat")
    raw = _read_text(path)
    if not raw:
        return None
    # comm is parenthesised and may itself contain spaces and ')', so the only
    # safe split is on the *last* ')'.
    head, _, tail = raw.partition(" (")
    comm, _, rest = tail.rpartition(") ")
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        # fields[0] is field 3 (state) in proc(5) numbering.
        return Stat(
            pid=int(head),
            comm=comm,
            state=fields[0],
            num_threads=int(fields[17]),
            utime_ticks=int(fields[11]),
            stime_ticks=int(fields[12]),
            start_time_ticks=int(fields[19]),
        )
    except (ValueError, IndexError):
        return None


def read_comm(pid: int, tid: Optional[int] = None) -> Optional[str]:
    path = proc_path(pid, "comm") if tid is None else task_path(pid, tid, "comm")
    raw = _read_text(path)
    return raw.strip() if raw else None


def read_cmdline(pid: int) -> List[str]:
    raw = _read_text(proc_path(pid, "cmdline"))
    if not raw:
        return []
    return [part for part in raw.split("\0") if part]


def read_exe(pid: int) -> Optional[str]:
    try:
        return os.readlink(str(proc_path(pid, "exe")))
    except OSError:
        return None


def read_cwd(pid: int) -> Optional[str]:
    try:
        return os.readlink(str(proc_path(pid, "cwd")))
    except OSError:
        return None


def thread_ids(pid: int) -> List[int]:
    try:
        return sorted(int(name) for name in os.listdir(str(proc_path(pid, "task"))))
    except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError, OSError):
        return []


def thread_count(pid: int) -> int:
    """Prefer the cheap counter in stat; fall back to listing the directory."""
    stat = read_stat(pid)
    if stat is not None:
        return stat.num_threads
    return len(thread_ids(pid))


# --------------------------------------------------------------------------
# scheduler statistics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedstat:
    run_ns: int
    wait_ns: int
    timeslices: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "run_ns": self.run_ns,
            "wait_ns": self.wait_ns,
            "timeslices": self.timeslices,
        }


def read_schedstat(pid: int, tid: Optional[int] = None) -> Optional[Schedstat]:
    """``run_ns wait_ns timeslices``. None when CONFIG_SCHEDSTATS is off."""
    path = proc_path(pid, "schedstat") if tid is None else task_path(pid, tid, "schedstat")
    raw = _read_text(path)
    if not raw:
        return None
    fields = raw.split()
    if len(fields) < 3:
        return None
    try:
        return Schedstat(int(fields[0]), int(fields[1]), int(fields[2]))
    except ValueError:
        return None


def read_ctxt_switches(pid: int, tid: Optional[int] = None) -> Tuple[Optional[int], Optional[int]]:
    """(voluntary, nonvoluntary) from ``status``."""
    path = proc_path(pid, "status") if tid is None else task_path(pid, tid, "status")
    raw = _read_text(path)
    if not raw:
        return None, None
    voluntary = nonvoluntary = None
    for line in raw.splitlines():
        if line.startswith("voluntary_ctxt_switches:"):
            voluntary = _int_or_none(line.split(":", 1)[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            nonvoluntary = _int_or_none(line.split(":", 1)[1])
    return voluntary, nonvoluntary


def _int_or_none(text: str) -> Optional[int]:
    try:
        return int(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def aggregate_schedstat(pid: int) -> Schedstat:
    """Sum every live thread's schedstat.

    The process level file only covers the main thread, so a 315 thread
    process would otherwise look almost idle.
    """
    run_ns = wait_ns = timeslices = 0
    for tid in thread_ids(pid):
        stat = read_schedstat(pid, tid)
        if stat is None:
            continue
        run_ns += stat.run_ns
        wait_ns += stat.wait_ns
        timeslices += stat.timeslices
    return Schedstat(run_ns, wait_ns, timeslices)


def aggregate_ctxt_switches(pid: int) -> Tuple[int, int]:
    voluntary = nonvoluntary = 0
    for tid in thread_ids(pid):
        vol, nonvol = read_ctxt_switches(pid, tid)
        voluntary += vol or 0
        nonvoluntary += nonvol or 0
    return voluntary, nonvoluntary


# --------------------------------------------------------------------------
# thread inventory  (meta/threads.json)
# --------------------------------------------------------------------------


def snapshot_threads(pid: int) -> Dict[int, Dict[str, Any]]:
    """One pass over ``/proc/<pid>/task``: name plus schedstat per thread."""
    snapshot: Dict[int, Dict[str, Any]] = {}
    for tid in thread_ids(pid):
        name = read_comm(pid, tid)
        if name is None:
            continue  # thread exited mid-walk; nothing to record
        schedstat = read_schedstat(pid, tid)
        voluntary, nonvoluntary = read_ctxt_switches(pid, tid)
        entry: Dict[str, Any] = {"name": name}
        if schedstat is not None:
            stats = schedstat.to_dict()
            if voluntary is not None:
                stats["voluntary_ctxt_switches"] = voluntary
            if nonvoluntary is not None:
                stats["nonvoluntary_ctxt_switches"] = nonvoluntary
            entry["schedstat"] = stats
        else:
            entry["schedstat"] = None
        snapshot[tid] = entry
    return snapshot


def merge_thread_snapshots(
    start: Dict[int, Dict[str, Any]], end: Dict[int, Dict[str, Any]]
) -> Dict[str, Any]:
    """Build the ``threads`` object of ``meta/threads.json``.

    Threads that appeared during the run and threads that exited during it are
    both kept: in a pool that churns, the ones that came and went are often the
    interesting ones.
    """
    threads: Dict[str, Any] = {}
    for tid in sorted(set(start) | set(end)):
        in_start = tid in start
        in_end = tid in end
        source = start.get(tid) or end.get(tid) or {}
        entry: Dict[str, Any] = {
            "name": (end.get(tid) or source).get("name", ""),
            "first_seen": "start" if in_start else "end",
        }
        if in_start and not in_end:
            entry["exited"] = True
        if in_start:
            entry["start_schedstat"] = start[tid].get("schedstat")
        if in_end:
            entry["end_schedstat"] = end[tid].get("schedstat")
        threads[str(tid)] = entry
    return threads


# --------------------------------------------------------------------------
# CPU sampling  (the overhead estimate)
# --------------------------------------------------------------------------


def cpu_ticks(pid: int) -> Optional[Tuple[int, int]]:
    stat = read_stat(pid)
    if stat is None:
        return None
    return stat.utime_ticks, stat.stime_ticks


def sample_cpu(pid: int, window_s: float, *, sleep=time.sleep) -> Optional[Dict[str, Any]]:
    """Measure target CPU usage over ``window_s`` seconds.

    Crude on purpose: utime+stime either side of a wall clock window.  It is
    not an accurate profile of anything, but the *difference* between the
    before and after samples is a usable estimate of what the measurement
    cost, which is all the manifest claims.
    """
    first = cpu_ticks(pid)
    if first is None:
        return None
    started = time.monotonic()
    sleep(window_s)
    elapsed = time.monotonic() - started
    second = cpu_ticks(pid)
    if second is None or elapsed <= 0:
        return None
    used_ticks = (second[0] - first[0]) + (second[1] - first[1])
    return {
        "window_s": round(elapsed, 3),
        "utime_ticks": second[0] - first[0],
        "stime_ticks": second[1] - first[1],
        "cpu_pct": round(100.0 * (used_ticks / CLK_TCK) / elapsed, 2),
    }


def mean_cpu_pct(samples: Sequence[Optional[Dict[str, Any]]]) -> Optional[float]:
    values = [s["cpu_pct"] for s in samples if s and s.get("cpu_pct") is not None]
    return sum(values) / len(values) if values else None


def overhead_pct(baseline_pct: Optional[float], during_pct: Optional[float]) -> float:
    """Percentage of target CPU added by the measurement.

    The baseline is the average of the untraced samples taken either side of
    the run; ``during_pct`` is the target's CPU over the traced window itself.
    Comparing before with after would compare two untraced states and always
    report roughly zero -- the cost has to be measured while it is being paid.

    Expressed relative to the baseline, so it reads as "the workload became N%
    more expensive". Negative results clamp to zero: tracing cannot make the
    target cheaper, so a negative number is baseline noise.
    """
    if not baseline_pct or during_pct is None or baseline_pct <= 0:
        return 0.0
    return max(0.0, round(100.0 * (during_pct - baseline_pct) / baseline_pct, 2))


def cpu_pct_between(
    before_ticks: Optional[Tuple[int, int]],
    after_ticks: Optional[Tuple[int, int]],
    elapsed_s: float,
) -> Optional[float]:
    """CPU percentage across an arbitrary window from two tick readings."""
    if before_ticks is None or after_ticks is None or elapsed_s <= 0:
        return None
    used = (after_ticks[0] - before_ticks[0]) + (after_ticks[1] - before_ticks[1])
    if used < 0:
        return None
    return round(100.0 * (used / CLK_TCK) / elapsed_s, 2)


# --------------------------------------------------------------------------
# host environment  (meta/system.json)
# --------------------------------------------------------------------------


def _sysctl_int(name: str) -> Optional[int]:
    raw = _read_text(PROC / "sys" / name.replace(".", "/"))
    return _int_or_none(raw) if raw else None


def _os_release() -> Optional[str]:
    raw = _read_text(Path("/etc/os-release"))
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _cpu_model() -> Optional[str]:
    raw = _read_text(PROC / "cpuinfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def _mem_total_kb() -> Optional[int]:
    raw = _read_text(PROC / "meminfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("MemTotal:"):
            return _int_or_none(line.split(":", 1)[1])
    return None


def _boot_time() -> Optional[str]:
    raw = _read_text(PROC / "stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("btime "):
            seconds = _int_or_none(line.split(None, 1)[1])
            if seconds is None:
                return None
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    return None


def cgroup_info(pid: int) -> Optional[Dict[str, Optional[str]]]:
    """cgroup v2 limits for the target.

    A CPU quota changes what "saturated" means, so a run measured inside a
    two-core quota on an eight-core box is not comparable with one that was
    not, and the viewer needs to be able to say so.
    """
    raw = _read_text(proc_path(pid, "cgroup"))
    if not raw:
        return None
    relative = None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":  # v2 unified hierarchy
            relative = parts[2]
            break
    if relative is None:
        return {"path": None, "cpu_max": None, "memory_max": None, "cpuset_cpus": None}
    base = Path("/sys/fs/cgroup") / relative.lstrip("/")

    def _value(name: str) -> Optional[str]:
        text = _read_text(base / name)
        return text.strip() if text else None

    return {
        "path": relative,
        "cpu_max": _value("cpu.max"),
        "memory_max": _value("memory.max"),
        "cpuset_cpus": _value("cpuset.cpus.effective") or _value("cpuset.cpus"),
    }


def nofile_limit() -> Tuple[int, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft, hard


def system_info(pid: Optional[int] = None) -> Dict[str, Any]:
    uname = os.uname()
    try:
        load = list(os.getloadavg())
    except OSError:  # pragma: no cover
        load = None
    soft, _hard = nofile_limit()
    return {
        "schema_version": 1,
        "hostname": uname.nodename or None,
        "kernel": uname.release,
        "arch": uname.machine,
        "distro": _os_release(),
        "cpu_count": len(os.sched_getaffinity(0)) or os.cpu_count() or 1,
        "cpu_model": _cpu_model(),
        "boot_time": _boot_time(),
        "mem_total_kb": _mem_total_kb(),
        "clk_tck": CLK_TCK,
        "perf_event_paranoid": _sysctl_int("kernel.perf_event_paranoid"),
        "kptr_restrict": _sysctl_int("kernel.kptr_restrict"),
        "ulimit_nofile": soft if soft != resource.RLIM_INFINITY else None,
        "cgroup": cgroup_info(pid) if pid is not None else None,
        "load_avg": load,
    }


def target_info(pid: int) -> Dict[str, Any]:
    """The static half of ``meta/target.json``."""
    stat = read_stat(pid)
    return {
        "schema_version": 1,
        "pid": pid,
        "comm": (stat.comm if stat else read_comm(pid)) or "unknown",
        "cmdline": read_cmdline(pid),
        "exe": read_exe(pid),
        "cwd": read_cwd(pid),
        "start_time_ticks": stat.start_time_ticks if stat else None,
        "thread_count_start": 0,
        "thread_count_end": 0,
    }


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def is_same_process(pid: int, start_time_ticks: Optional[int]) -> bool:
    """Guard against pid recycling.

    Between preflight and the end of a 60 second run the target can exit and
    a new process can be handed the same pid.  ``start_time_ticks`` pins the
    instance, so the collector notices rather than attributing a stranger's
    threads to the target.
    """
    if start_time_ticks is None:
        return is_running(pid)
    stat = read_stat(pid)
    return (
        stat is not None
        and stat.state not in _DEAD_STATES
        and stat.start_time_ticks == start_time_ticks
    )


def have_capabilities() -> Tuple[bool, Dict[str, bool]]:
    """Can this process attach eBPF programs?

    Root is the simple answer.  Otherwise CAP_BPF plus CAP_PERFMON is enough
    on 5.8+, and CAP_SYS_ADMIN covers everything on older kernels.
    """
    if os.geteuid() == 0:
        return True, {"root": True}
    raw = _read_text(PROC / "self" / "status") or ""
    effective = 0
    for line in raw.splitlines():
        if line.startswith("CapEff:"):
            try:
                effective = int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                effective = 0
            break
    caps = {
        "root": False,
        "cap_sys_admin": bool(effective & (1 << 21)),
        "cap_perfmon": bool(effective & (1 << 38)),
        "cap_bpf": bool(effective & (1 << 39)),
    }
    ok = caps["cap_sys_admin"] or (caps["cap_bpf"] and caps["cap_perfmon"])
    return ok, caps
