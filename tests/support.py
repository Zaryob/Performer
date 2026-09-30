"""Shared test helpers: a real multi threaded target and a fake bpftrace.

There is no bpftrace and no tracefs in a normal CI container, so the collector
is exercised against a test double that reproduces the behaviours the code
actually depends on -- above all that a probe writes its maps only when it is
SIGINTed.  Everything else in the pipeline (process groups, signal escalation,
/proc readers, parsing, bundle writing) runs for real, against a real process
with real threads.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Iterator

from . import REPO_ROOT

TARGET_SOURCE = REPO_ROOT / "tests" / "target" / "contention.cc"
TARGET_BIN = REPO_ROOT / "tests" / "target" / "contention"
FAKE_BPFTRACE = REPO_ROOT / "tests" / "fake_bpftrace.py"


def target_built() -> bool:
    return TARGET_BIN.is_file() and os.access(TARGET_BIN, os.X_OK)


requires_target = unittest.skipUnless(
    target_built(), f"synthetic target not built; run: make -C {TARGET_BIN.parent}"
)


class Target:
    """A running instance of ``tests/target/contention``."""

    def __init__(self, process: subprocess.Popen, pid: int, threads: int) -> None:
        self.process = process
        self.pid = pid
        self.threads = threads

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


@contextlib.contextmanager
def spawn_target(
    *,
    threads: int = 8,
    seconds: int = 60,
    contention: int = 80,
    hold_us: int = 20,
    sleep_us: int = 200,
    ready_timeout: float = 20.0,
) -> Iterator[Target]:
    """Start the C++ target and wait until its threads actually exist.

    Waits for the program's own "ready" line rather than sleeping, so the
    thread count is real by the time a test looks at it.
    """
    argv = [
        str(TARGET_BIN),
        "--threads", str(threads),
        "--seconds", str(seconds),
        "--contention", str(contention),
        "--hold-us", str(hold_us),
        "--sleep-us", str(sleep_us),
    ]
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            line = process.stdout.readline() if process.stdout else ""
            if not line and process.poll() is not None:
                raise RuntimeError("target exited before becoming ready")
            if line.strip() == "ready":
                break
        else:
            raise RuntimeError("target did not become ready in time")
        yield Target(process, process.pid, threads)
    finally:
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()


@contextlib.contextmanager
def fake_bpftrace(mode: str = "normal", *, version: str = "0.20.2", comm: str = "target"):
    """Put a fake ``bpftrace`` on PATH for the duration of the block.

    ``mode`` selects the failure being simulated; see ``fake_bpftrace.py``.
    """
    directory = Path(tempfile.mkdtemp(prefix="performer-fakebt-"))
    binary = directory / "bpftrace"
    shutil.copy2(FAKE_BPFTRACE, binary)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    previous = {
        "PATH": os.environ.get("PATH", ""),
        "FAKE_BPFTRACE_MODE": os.environ.get("FAKE_BPFTRACE_MODE"),
        "FAKE_BPFTRACE_VERSION": os.environ.get("FAKE_BPFTRACE_VERSION"),
        "FAKE_BPFTRACE_COMM": os.environ.get("FAKE_BPFTRACE_COMM"),
    }
    os.environ["PATH"] = f"{directory}{os.pathsep}{previous['PATH']}"
    os.environ["FAKE_BPFTRACE_MODE"] = mode
    os.environ["FAKE_BPFTRACE_VERSION"] = version
    os.environ["FAKE_BPFTRACE_COMM"] = comm
    try:
        yield str(binary)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(directory, ignore_errors=True)


def python_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    """A trivial child process, for tests that only need something with a pid."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launcher_chain(seconds: float = 30.0) -> subprocess.Popen:
    """sh -> sh -> python, like sudo -> sudo -> program under ``use_pty``.

    The trailing ``; true`` keeps each shell from exec'ing its command, so the
    shells stay in the process tree as parents.
    """
    inner = f"{sys.executable} -c 'import time; time.sleep({seconds})'; true"
    return subprocess.Popen(
        ["sh", "-c", f'sh -c "{inner}"; true'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
