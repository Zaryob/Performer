#!/usr/bin/env python3
"""A stand-in for bpftrace, for testing the collector without eBPF.

It reproduces the behaviours the collector actually depends on, which are the
ones that are awkward to get right:

  * it prints ``Attaching N probes...`` and then **nothing** until it is
    stopped -- the single write moment that makes SIGINT handling critical;
  * on SIGINT it dumps its maps to stdout and exits 0;
  * it accepts the same positional parameters as a ``.bt`` program;
  * ``--version`` answers like bpftrace does.

``FAKE_BPFTRACE_MODE`` selects the failure being simulated:

    normal          attach, wait, dump maps on SIGINT              (default)
    startup_error   fail to attach, like a bad tracepoint
    silent          attach, but produce no map output at all
    ignore_sigint   ignore SIGINT, forcing the SIGTERM escalation
    stubborn        ignore SIGINT and SIGTERM, forcing SIGKILL
    lost_events     normal, but report dropped events on stderr
    empty_stacks    normal, but every stack is unresolved

This is a test double, not a simulator: it makes no attempt to be bpftrace.
"""

from __future__ import annotations

import os
import signal
import sys
import time

VERSION = os.environ.get("FAKE_BPFTRACE_VERSION", "0.20.2")

MAP_OUTPUT = """@cpu[
    __schedule+723
    schedule+70
    futex_wait_queue_me+164
,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
    start_thread+219
, {comm}]: 1493
@cpu[
    finish_task_switch+123
,
    EventQueue::pop()+44
    WorkerThread::run()+1080
    start_thread+219
, worker]: 842
@cpu[
,
    UserLogic::compute()+96
    WorkerThread::run()+1080
    start_thread+219
, worker]: 611
"""

EMPTY_STACK_OUTPUT = """@cpu[
,
    0x7f3c8a4419a1
    0x7f3c8a2f1177
, worker]: 900
@cpu[
    finish_task_switch+123
,
    WorkerThread::run()+1080
, worker]: 100
"""

_stop = False


def _on_sigint(_signum, _frame) -> None:
    global _stop
    _stop = True


def main(argv: list) -> int:
    if "--version" in argv:
        print(f"bpftrace v{VERSION}")
        return 0

    mode = os.environ.get("FAKE_BPFTRACE_MODE", "normal")
    comm = os.environ.get("FAKE_BPFTRACE_COMM", "target")

    if mode == "startup_error":
        # bpftrace reports attach failures on stderr and exits immediately.
        print("stdin:1:1-20: ERROR: Invalid provider: 'nosuchprobe'", file=sys.stderr)
        return 1

    positional = [a for a in argv[1:] if not a.startswith("-")]
    watchdog = 3600.0
    if len(positional) >= 3:
        try:
            watchdog = float(positional[2])
        except ValueError:
            pass

    signal.signal(signal.SIGINT, _on_sigint)
    if mode in ("ignore_sigint", "stubborn"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    print(f"Attaching {len(positional)} probes...", flush=True)

    deadline = time.monotonic() + watchdog
    while not _stop and time.monotonic() < deadline:
        time.sleep(0.05)

    if mode == "lost_events":
        print("Lost 12043 events", file=sys.stderr, flush=True)
    if mode == "silent":
        return 0

    print()
    if mode == "empty_stacks":
        sys.stdout.write(EMPTY_STACK_OUTPUT)
    else:
        sys.stdout.write(MAP_OUTPUT.format(comm=comm))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
