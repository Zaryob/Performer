#!/usr/bin/env python3
"""A stand-in for bpftrace, for testing the collector without eBPF.

It reproduces the behaviours the collector actually depends on, which are the
ones that are awkward to get right:

  * it prints ``Attaching N probes...`` and then **nothing** until it is
    stopped -- the single write moment that makes SIGINT handling critical;
  * on SIGINT it dumps its maps to stdout and exits 0;
  * the maps it dumps match the probe it was asked to run, in the shapes real
    bpftrace uses: stack maps, histograms, stats and plain value maps;
  * it accepts the same positional parameters as a ``.bt`` program;
  * ``--version`` answers like bpftrace does.

``FAKE_BPFTRACE_MODE`` selects the failure being simulated:

    normal          attach, wait, dump maps on SIGINT              (default)
    startup_error   fail to attach, like a bad tracepoint
    oncpu_startup_error  only the on-CPU probe fails to attach
    silent          attach, but produce no map output at all
    threadlife_silent  only threadlife has no fork/exit events
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

ONCPU = """@cpu[
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

OFFCPU = """@offcpu_us[
    __schedule+723
    schedule+70
    futex_wait_queue_me+164
,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
    start_thread+219
, {comm}]: 4820103
@offcpu_us[
    __schedule+723
    schedule+70
,
    pthread_cond_wait+512
    EventQueue::pop()+44
    WorkerThread::run()+1080
, worker]: 1260044
@offcpu_by_state[1]: 6041210
@offcpu_by_state[2]: 38900
@offcpu_hist:
[64, 128)             12 |@@                                                  |
[128, 256)           190 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@                      |
[256, 512)           318 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[1K, 2K)              44 |@@@@@@@                                             |
[1M, ...)              2 |                                                    |

@_off_kstack[4243]: 
        __schedule+723
        schedule+70

@_off_start[4243]: 91838741177
@_off_state[4243]: 1
@_off_ustack[4243]: 
        pthread_cond_wait+512
        EventQueue::pop()+44

"""

RUNQLAT = """@_queued_at[4242]: 91838740021
@_queued_at[4243]: 91838741177

@runq_us:
[0]                   93 |@@@@@                                               |
[1]                  842 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[2, 4)               611 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@                 |
[4, 8)               204 |@@@@@@@@@@@@                                        |
[64, 128)             18 |@                                                   |
@runq_by_thread[{comm}]: count 421, average 3, total 1263
@runq_by_thread[worker]: count 1347, average 2, total 2694
"""

FUTEX = """@futex_us[
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
, {comm}]: 4760000
@futex_us[
    pthread_cond_wait+512
    EventQueue::pop()+44
    WorkerThread::run()+1080
, worker]: 1180000
@futex_by_addr[139904315130432]: 6820000
@futex_by_addr[139904315131208]: 118400
@futex_by_addr[139904315133456]: 9200
@futex_cnt_by_addr[139904315130432]: 41920
@futex_cnt_by_addr[139904315131208]: 2140
@futex_cnt_by_addr[139904315133456]: 310
@futex_site[139904315130432,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
]: 6180000
@futex_site[139904315130432,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::cancel(unsigned long)+96
    WorkerThread::run()+1080
]: 640000
@futex_site[139904315131208,
    pthread_cond_wait+512
    EventQueue::pop()+44
    WorkerThread::run()+1080
]: 118400
@futex_site[139904315133456,
    pthread_cond_wait+512
    Dispatcher::run()+220
]: 9200
@futex_site_cnt[139904315130432,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
]: 38100
@futex_site_cnt[139904315130432,
    __lll_lock_wait+40
    pthread_mutex_lock+274
    TimerWheel::cancel(unsigned long)+96
    WorkerThread::run()+1080
]: 3820
@futex_site_cnt[139904315131208,
    pthread_cond_wait+512
    EventQueue::pop()+44
    WorkerThread::run()+1080
]: 2140
@futex_site_cnt[139904315133456,
    pthread_cond_wait+512
    Dispatcher::run()+220
]: 310
@futex_hist:
[32, 64)             410 |@@@@@@@@@@@                                         |
[64, 128)           1820 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[128, 256)           904 |@@@@@@@@@@@@@@@@@@@@@@@@                            |
[8K, 16K)             12 |                                                    |
"""

WAKEUP = """@wake_cnt[4101, 4102]: 5200
@wake_cnt[4101, 4103]: 4870
@wake_cnt[4101, 4104]: 4610
@wake_cnt[4102, 4101]: 210
@wake_cnt[4103, 4101]: 190
@wake_from_comm[{comm}]: 14680
@wake_to_comm[worker]: 14290
"""

SYSCALL_LAT = """@sc_total_us[202]: 7912400
@sc_total_us[232]: 3204100
@sc_total_us[230]: 2410800
@sc_total_us[0]: 184300
@sc_total_us[9999]: 120
@sc_count[202]: 128400
@sc_count[232]: 18900
@sc_count[230]: 61200
@sc_count[0]: 44100
@sc_count[9999]: 4
"""

THREADLIFE = """@fork_cnt[{comm}]: 42
@exit_cnt[worker]: 39
@thread_lifetime_ms:
[256, 512)            18 |@@@@@@@@@@@@@@@@@@@@@@@@@                           |
[512, 1K)             36 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@  |
[1K, 2K)               3 |@@@@                                                |
"""

TIMERS = """@timer_calls[tracepoint:syscalls:sys_enter_clock_nanosleep]: 61200
@timer_calls[tracepoint:syscalls:sys_enter_timerfd_settime]: 121400
@timer_calls[tracepoint:syscalls:sys_enter_epoll_wait]: 18900
@timer_arm_stack[
    timerfd_settime+12
    TimerWheel::arm(unsigned long, std::function<void ()>)+188
    WorkerThread::run()+1080
]: 121400
"""

#: Probe program name -> the maps that probe prints.
OUTPUT_BY_PROBE = {
    "oncpu.bt": ONCPU,
    "offcpu.bt": OFFCPU,
    "runqlat.bt": RUNQLAT,
    "futex.bt": FUTEX,
    "wakeup.bt": WAKEUP,
    "syscall_lat.bt": SYSCALL_LAT,
    "threadlife.bt": THREADLIFE,
    "timers.bt": TIMERS,
}

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

    positional = [a for a in argv[1:] if not a.startswith("-")]
    program = os.path.basename(positional[0]) if positional else ""

    if mode == "startup_error" or (mode == "oncpu_startup_error" and program == "oncpu.bt"):
        # bpftrace reports attach failures on stderr and exits immediately.
        print("stdin:1:1-20: ERROR: Invalid provider: 'nosuchprobe'", file=sys.stderr)
        return 1

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
    if mode == "silent" or (mode == "threadlife_silent" and program == "threadlife.bt"):
        return 0

    print()
    if mode == "empty_stacks":
        body = EMPTY_STACK_OUTPUT
    else:
        # An unknown program still gets the on-CPU shape, so a test that
        # invents a probe name is not silently given nothing.
        body = OUTPUT_BY_PROBE.get(program, ONCPU)
    sys.stdout.write(body.format(comm=comm))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
