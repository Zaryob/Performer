"""Syscall number -> name.

The probe records numbers because carrying a string per event would cost far
more than the measurement is worth; the mapping happens here, where it is
free.

The number depends on the architecture, so this reads the target host's own
headers first and only falls back to a built-in table.  Which source was used
is reported, because a wrong table would silently mislabel every row -- and a
mislabelled ``futex`` is exactly the kind of error that sends someone looking
in the wrong place for an afternoon.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

_DEFINE_RE = re.compile(r"^#define\s+__NR_([A-Za-z0-9_]+)\s+(\d+)")

#: Header locations, in order of preference.  The first is the x86_64 table
#: and the second the architecture independent include.
_HEADER_CANDIDATES = (
    "/usr/include/asm/unistd_64.h",
    "/usr/include/x86_64-linux-gnu/asm/unistd_64.h",
    "/usr/include/asm-generic/unistd.h",
    "/usr/include/asm/unistd.h",
)

#: Fallback for x86_64, covering what a threaded application actually calls.
#: Deliberately not exhaustive: an entry that is not here renders as
#: ``syscall_<n>``, which is honest, whereas a guessed name is not.
_X86_64: Dict[int, str] = {
    0: "read", 1: "write", 2: "open", 3: "close", 4: "stat", 5: "fstat",
    6: "lstat", 7: "poll", 8: "lseek", 9: "mmap", 10: "mprotect", 11: "munmap",
    12: "brk", 13: "rt_sigaction", 14: "rt_sigprocmask", 15: "rt_sigreturn",
    16: "ioctl", 17: "pread64", 18: "pwrite64", 19: "readv", 20: "writev",
    21: "access", 22: "pipe", 23: "select", 24: "sched_yield", 25: "mremap",
    26: "msync", 27: "mincore", 28: "madvise", 32: "dup", 33: "dup2",
    34: "pause", 35: "nanosleep", 36: "getitimer", 37: "alarm",
    38: "setitimer", 39: "getpid", 40: "sendfile", 41: "socket", 42: "connect",
    43: "accept", 44: "sendto", 45: "recvfrom", 46: "sendmsg", 47: "recvmsg",
    48: "shutdown", 49: "bind", 50: "listen", 51: "getsockname",
    52: "getpeername", 53: "socketpair", 54: "setsockopt", 55: "getsockopt",
    56: "clone", 57: "fork", 58: "vfork", 59: "execve", 60: "exit",
    61: "wait4", 62: "kill", 63: "uname", 72: "fcntl", 73: "flock",
    74: "fsync", 75: "fdatasync", 76: "truncate", 77: "ftruncate",
    78: "getdents", 79: "getcwd", 80: "chdir", 87: "unlink", 89: "readlink",
    96: "gettimeofday", 97: "getrlimit", 98: "getrusage", 99: "sysinfo",
    100: "times", 102: "getuid", 104: "getgid", 105: "setuid", 107: "geteuid",
    108: "getegid", 109: "setpgid", 110: "getppid", 137: "statfs",
    158: "arch_prctl", 186: "gettid", 200: "tkill", 202: "futex",
    213: "epoll_create", 217: "getdents64", 218: "set_tid_address",
    228: "clock_gettime", 229: "clock_getres", 230: "clock_nanosleep",
    231: "exit_group", 232: "epoll_wait", 233: "epoll_ctl", 234: "tgkill",
    257: "openat", 262: "newfstatat", 263: "unlinkat", 267: "readlinkat",
    270: "pselect6", 271: "ppoll", 273: "set_robust_list",
    274: "get_robust_list", 281: "epoll_pwait", 283: "timerfd_create",
    286: "timerfd_settime", 287: "timerfd_gettime", 288: "accept4",
    290: "eventfd2", 291: "epoll_create1", 292: "dup3", 293: "pipe2",
    302: "prlimit64", 318: "getrandom", 332: "statx", 425: "io_uring_setup",
    435: "clone3", 439: "faccessat2",
}


def _load_from_headers() -> Tuple[Optional[Dict[int, str]], Optional[str]]:
    for candidate in _HEADER_CANDIDATES:
        path = Path(candidate)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        table: Dict[int, str] = {}
        for line in text.splitlines():
            match = _DEFINE_RE.match(line)
            if match is None:
                continue
            number = int(match.group(2))
            # The headers define aliases (__NR_fstatat64 alongside
            # __NR_newfstatat); the first name wins so the result is stable.
            table.setdefault(number, match.group(1))
        if len(table) > 50:
            return table, candidate
    return None, None


class SyscallTable:
    """Number to name, with the provenance of the mapping."""

    def __init__(self, table: Dict[int, str], source: str) -> None:
        self._table = table
        self.source = source

    def name(self, number: int) -> str:
        return self._table.get(number, f"syscall_{number}")

    def known(self, number: int) -> bool:
        return number in self._table

    def __len__(self) -> int:
        return len(self._table)


_cached: Optional[SyscallTable] = None


def load(*, prefer_headers: bool = True) -> SyscallTable:
    """Build the table once per process."""
    global _cached
    if _cached is not None:
        return _cached
    if prefer_headers:
        table, source = _load_from_headers()
        if table is not None:
            _cached = SyscallTable(table, source or "system headers")
            return _cached
    machine = os.uname().machine
    _cached = SyscallTable(
        dict(_X86_64), f"built-in x86_64 table (host is {machine})"
    )
    return _cached


def reset_cache() -> None:
    """For tests that need to exercise both sources."""
    global _cached
    _cached = None
