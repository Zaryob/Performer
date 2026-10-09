"""Synthetic bundle generation.

M0 has no probes yet, but the bundle format has to be nailed down before any
of them are written.  This module produces a complete, schema valid bundle
from made up data, which serves three purposes:

  * it proves the layout is actually writable through :class:`BundleBuilder`;
  * it gives ``performer inspect`` something to read on a machine with no
    bpftrace, no root and no target process;
  * it gives the viewer (M3) and the diff work (M4) real input to develop
    against before the collector exists.

The numbers are invented but the *shape* is not: 315 threads, one dominant
futex address, a timer manager at the centre of the wakeup graph.  Data
generated here is deterministic for a given seed.
"""

from __future__ import annotations

from . import provenance

import datetime as _dt
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import layout, pmu as pmu_mod
from .bundle import BundleBuilder
from .errors import PerformerError
from .manifest import (
    ProbeResult,
    Quality,
    TargetInfo,
    build_manifest,
    utc_now,
)

DEFAULT_COMM = "app"
DEFAULT_PID = 205852
HOT_FUTEX_ADDR = 0x7F3C8A001240

_ONCPU_STACKS: Sequence[Tuple[str, int]] = (
    ("[unknown]", 40),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::tick();"
     "TimerWheel::arm();__lll_lock_wait", 900),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::tick();"
     "TimerWheel::insertDelta()", 620),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();EventQueue::pop();"
     "operator new;_int_malloc", 480),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();EventQueue::pop();"
     "operator new;arena_get2", 310),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();UserLogic::onEvent();"
     "UserLogic::compute()", 1450),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();EventQueue::push();"
     "pthread_cond_signal", 260),
    (f"{DEFAULT_COMM};main;Dispatcher::run();epoll_wait", 90),
)

_OFFCPU_STACKS: Sequence[Tuple[str, int]] = (
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::arm();"
     "pthread_mutex_lock;__lll_lock_wait;futex_wait", 4_820_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::cancel();"
     "pthread_mutex_lock;__lll_lock_wait;futex_wait", 2_140_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();EventQueue::pop();"
     "pthread_cond_wait;futex_wait", 1_260_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();clock_nanosleep", 980_000),
    (f"{DEFAULT_COMM};main;Dispatcher::run();epoll_wait", 610_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();read;sock_recvmsg", 155_000),
)

_FUTEX_STACKS: Sequence[Tuple[str, int]] = (
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::arm();"
     "pthread_mutex_lock", 4_760_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();TimerWheel::cancel();"
     "pthread_mutex_lock", 2_090_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();EventQueue::pop();"
     "pthread_cond_wait", 1_180_000),
    (f"{DEFAULT_COMM};start_thread;WorkerThread::run();MemPool::allocate();"
     "pthread_mutex_lock", 120_000,),
)

# --------------------------------------------------------------------------
# load scenarios
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadScenario:
    """The same application, working harder.

    A diff needs two runs to compare, and two runs that differ only by a
    random seed are a poor test of one: the differences are noise, evenly
    spread, with nothing a correct tool should surface above anything else.
    This models the change an operator is actually looking for -- a workload
    that pushed the same code down the same paths at different rates, and one
    path that did not exist at the lighter load at all.

    Which paths grow is not arbitrary either.  They are the three the project
    exists to investigate: one shared timer mutex, the glibc malloc arena, and
    idle time that disappears as the process saturates.
    """

    description: str
    #: Substring of a stack template -> multiplier for its weight.  The first
    #: match wins, so more specific patterns come first.
    scale: Sequence[Tuple[str, float]] = ()
    #: Stack kind -> call paths that exist only at this load.  Real load does
    #: not merely make the same code slower; past a threshold it takes
    #: different code, and that is the finding a diff should make obvious.
    extra: Dict[str, Sequence[Tuple[str, int]]] = field(default_factory=dict)


LOAD_SCENARIOS: Dict[str, LoadScenario] = {
    "steady": LoadScenario(description="the reference workload"),
    # The negative control. A tool that names a bottleneck is only worth
    # having if it also declines to when there is not one, and that is a
    # property nothing else in the fixture set exercises: every other scenario
    # has the same deliberately hot mutex in it. Only the lock contention is
    # damped here -- the run still has plenty going on, which is the point:
    # "no *lock* to blame" has to survive a profile that is otherwise busy.
    "spread": LoadScenario(
        description="lock cost spread across many addresses; no one mutex to blame",
        scale=(("TimerWheel::arm", 0.35), ("TimerWheel::cancel", 0.5)),
    ),
    "heavy": LoadScenario(
        description="the same process under roughly twice the message rate",
        scale=(
            # The single timer mutex is the suspected bottleneck, and it is
            # superlinear: twice the work, three times the contention.
            ("TimerWheel::arm", 3.2),
            ("TimerWheel::cancel", 2.6),
            ("TimerWheel::tick", 1.4),
            # Arena contention grows with allocating threads, not allocations.
            ("arena_get2", 2.9),
            ("_int_malloc", 1.9),
            ("MemPool::allocate", 2.2),
            # Useful work grows roughly with the rate.
            ("UserLogic::compute", 1.8),
            ("EventQueue", 1.7),
            # And the idle waits shrink as the process saturates.
            ("epoll_wait", 0.35),
            ("clock_nanosleep", 0.4),
        ),
        extra={
            # Backpressure: code that simply does not run while the queue
            # keeps up. A pair of flame graphs side by side would not make
            # this obvious; a diff should.
            "oncpu": (
                (f"{DEFAULT_COMM};start_thread;WorkerThread::run();"
                 "EventQueue::push();EventQueue::grow();operator new;"
                 "_int_malloc;arena_get2", 540),
                (f"{DEFAULT_COMM};start_thread;WorkerThread::run();"
                 "Backpressure::throttle();clock_gettime", 210),
            ),
            "offcpu": (
                (f"{DEFAULT_COMM};start_thread;WorkerThread::run();"
                 "Backpressure::throttle();pthread_cond_wait;futex_wait",
                 1_380_000),
            ),
            "futex": (
                (f"{DEFAULT_COMM};start_thread;WorkerThread::run();"
                 "Backpressure::throttle();pthread_cond_wait", 1_340_000),
            ),
        },
    ),
}

DEFAULT_LOAD = "steady"


def _apply_load(
    templates: Sequence[Tuple[str, int]], scenario: LoadScenario, kind: str
) -> List[Tuple[str, int]]:
    scaled: List[Tuple[str, int]] = []
    for stack, base in templates:
        factor = 1.0
        for needle, multiplier in scenario.scale:
            if needle in stack:
                factor = multiplier
                break
        scaled.append((stack, max(1, int(base * factor))))
    scaled.extend(scenario.extra.get(kind, ()))
    return scaled


_THREAD_NAME_POOL: Sequence[str] = (
    "TimerWheel",
    "event-disp",
    "sched-worker",
    "IoReactor",
    "SerialLink",
    "CanBus",
    "Telemetry",
    "Watchdog",
)


def _jitter(rng: random.Random, value: int, spread: float = 0.15) -> int:
    return max(1, int(value * (1.0 + rng.uniform(-spread, spread))))


#: Leaf frames appended to some stacks so the tree branches the way a real one
#: does, rather than being the same few paths repeated per thread.
_LEAF_VARIANTS: Sequence[str] = (
    "",
    ";memcpy",
    ";operator new(unsigned long);_int_malloc",
    ";std::_Rb_tree_increment(std::_Rb_tree_node_base const*)",
    ";__memmove_avx_unaligned_erms",
    ";std::vector<int, std::allocator<int> >::_M_realloc_insert(int const&)",
)


def _spread_over_threads(
    seed: int,
    kind: str,
    load: str,
    templates: Sequence[Tuple[str, int]],
    roster: Sequence[Tuple[int, str]],
    broken_stacks: bool = False,
) -> List[Tuple[str, int]]:
    """Turn a handful of stack templates into a per thread profile.

    The templates carry the shape -- which call paths exist and roughly how
    expensive each is -- and this spreads them across the real thread roster
    with the leaf variation and the long tail a genuine profile has.

    Every random choice is drawn from a stream keyed by *what it is about*
    rather than from one shared generator, and this is what makes the result
    usable as a diff fixture. Two bundles generated with the same seed and
    different loads have to be the same application doing the same things at
    different rates; with a single shared stream they are not, because adding
    one call path to a scenario shifts every subsequent draw and the two runs
    come out structurally unrelated. Keying each draw by ``(seed, thread,
    template)`` means the structure is identical between loads and only the
    load's own effect differs -- so what a diff finds is the load, which is
    the point. Run to run noise is still there: the jitter stream includes
    the load name, so every path carries an independent +/-15%, and a tool
    that cannot see past that is not much use on real measurements either.

    ``broken_stacks`` models a target built without frame pointers. That does
    not add unresolved stacks alongside good ones: it means the walker gets a
    frame or two and then gives up, so the *same* stacks come back truncated
    and capped with ``[unknown]``. Reproducing it that way is what makes the
    unknown frame ratio -- and therefore the viewer's red flag -- behave the
    way it will on a real unresolvable target.
    """
    folded: List[Tuple[str, int]] = []
    for tid, thread_name in roster:
        # Not every thread walks every path, and the busiest few dominate.
        weight = random.Random(f"{seed}:{kind}:{tid}").choice(
            (0.02, 0.05, 0.1, 0.3, 1.0, 1.0, 2.5)
        )
        for stack, base in templates:
            shape = random.Random(f"{seed}:{kind}:{tid}:{stack}")
            rng = random.Random(f"{seed}:{kind}:{load}:{tid}:{stack}")
            if shape.random() > 0.55:
                continue
            root, _, tail = stack.partition(";")
            # The template's own root is the process name, which the thread
            # name replaces -- except for a single frame template such as
            # "[unknown]", which has no process prefix to replace.
            frames = (tail if tail else root).split(";")
            # Which leaf a path grows, and where an unresolvable walk gives
            # up, are properties of the code rather than of the load, so they
            # come from the shape stream and stay put between scenarios.
            if broken_stacks:
                keep = shape.randint(0, 1)
                frames = frames[:keep] + ["[unknown]"] * shape.randint(1, 3)
            else:
                leaf = shape.choice(_LEAF_VARIANTS)
                if leaf:
                    frames = frames + leaf.lstrip(";").split(";")
            scaled = max(1, int(base * weight / max(1, len(roster) // 12)))
            folded.append(
                (";".join([thread_name, *frames]), _jitter(rng, scaled))
            )
    folded.sort(key=lambda item: (-item[1], item[0]))
    return folded


def _pow2_buckets(
    rng: random.Random, *, peak_us: int, total: int, high_tail: bool
) -> List[Dict[str, Any]]:
    """Power of two buckets shaped like a bpftrace ``hist()`` output."""
    buckets: List[Dict[str, Any]] = []
    lo = 0
    hi = 1
    remaining = total
    for step in range(20):
        centre = 1 << step
        distance = abs((centre or 1) / peak_us)
        weight = 1.0 / (1.0 + abs(1.0 - distance) * (2.0 if high_tail else 6.0))
        count = int(remaining * weight * rng.uniform(0.10, 0.30))
        count = max(0, min(count, remaining))
        buckets.append({"lo": lo, "hi": hi, "count": count})
        remaining -= count
        lo = hi
        hi = hi * 2
    buckets.append({"lo": lo, "hi": None, "count": remaining})
    return buckets


def _histogram_doc(
    name: str,
    unit: str,
    source: str,
    series: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": layout.SCHEMA_VERSION,
        "kind": "histogram",
        "name": name,
        "unit": unit,
        "source": source,
        "series": series,
    }


def _series(key: str, buckets: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = sum(b["count"] for b in buckets)
    weighted = sum(b["count"] * (b["lo"] or 0.5) for b in buckets)
    return {
        "key": key,
        "buckets": buckets,
        "total_count": total,
        "stats": {
            "count": total,
            "sum": weighted,
            "min": 1,
            "max": 65536,
            "avg": round(weighted / total, 2) if total else 0.0,
        },
    }


def _threads(
    rng: random.Random, *, pid: int, count: int, extra_at_end: int
) -> Tuple[Dict[str, Any], List[Tuple[int, str]]]:
    """Build meta/threads.json plus the (tid, name) list used elsewhere."""
    roster: List[Tuple[int, str]] = [(pid, DEFAULT_COMM)]
    tid = pid + 1
    for index in range(count - 1):
        name = _THREAD_NAME_POOL[index % len(_THREAD_NAME_POOL)]
        roster.append((tid, f"{name}{index // len(_THREAD_NAME_POOL)}"))
        tid += rng.randint(1, 3)

    threads: Dict[str, Any] = {}
    for position, (thread_id, name) in enumerate(roster):
        born_late = position >= len(roster) - extra_at_end and extra_at_end > 0
        run_ns = rng.randint(2_000_000, 900_000_000)
        wait_ns = rng.randint(1_000_000, 60_000_000)
        entry: Dict[str, Any] = {
            "name": name,
            "start_time_ticks": thread_id,
            "first_seen": "end" if born_late else "start",
        }
        if not born_late:
            entry["start_schedstat"] = {
                "run_ns": run_ns,
                "wait_ns": wait_ns,
                "timeslices": rng.randint(1000, 90_000),
            }
        entry["end_schedstat"] = {
            "run_ns": run_ns + rng.randint(1_000_000, 400_000_000),
            "wait_ns": wait_ns + rng.randint(100_000, 20_000_000),
            "timeslices": rng.randint(90_000, 200_000),
            "voluntary_ctxt_switches": rng.randint(500, 40_000),
            "nonvoluntary_ctxt_switches": rng.randint(10, 4_000),
        }
        threads[str(thread_id)] = entry
    return threads, roster


def generate(
    out_dir: Path,
    *,
    label: str = "synthetic",
    duration_s: int = 60,
    thread_count: int = 315,
    pid: int = DEFAULT_PID,
    profile: str = "standard",
    seed: int = 20260806,
    load: str = DEFAULT_LOAD,
    degraded: bool = False,
    bad_frame_pointers: bool = False,
    tags: Sequence[str] = ("synthetic",),
    notes: str = "",
    pack: bool = True,
    pmu: str = "off",
    started_at: Optional[_dt.datetime] = None,
) -> Tuple[Path, Path]:
    """Write a synthetic bundle.

    ``degraded`` produces the awkward case the viewer must handle: a probe
    that failed its smoke test, a probe that lost events, and a target that
    died mid-run.  ``load`` selects a :class:`LoadScenario`, which is how two
    bundles that differ the way two real measurements of the same process
    differ are produced.  Returns ``(run_dir, archive_or_run_dir)``.
    """
    try:
        scenario = LOAD_SCENARIOS[load]
    except KeyError:
        raise PerformerError(
            f"unknown load scenario {load!r}; available: "
            + ", ".join(sorted(LOAD_SCENARIOS))
        ) from None
    rng = random.Random(seed)
    started = started_at or utc_now()
    builder = BundleBuilder(out_dir, label=label, started_at=started)

    extra_threads = 3
    threads_doc, roster = _threads(
        rng, pid=pid, count=thread_count, extra_at_end=extra_threads
    )
    end_count = thread_count if not degraded else max(0, thread_count - 12)

    # -- meta ----------------------------------------------------------
    builder.add_json(
        layout.META_SYSTEM,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "hostname": "target-host",
            "kernel": "5.15.0-91-generic",
            "arch": "x86_64",
            "distro": "Ubuntu 22.04.3 LTS",
            "cpu_count": 8,
            "cpu_model": "Intel(R) Xeon(R) E-2288G CPU @ 3.70GHz",
            "boot_time": "2026-07-30T08:11:02Z",
            "mem_total_kb": 32_784_112,
            "clk_tck": 100,
            "perf_event_paranoid": 1,
            "kptr_restrict": 0,
            "ulimit_nofile": 1_048_576,
            "cgroup": {
                "path": "/system.slice/app.service",
                "cpu_max": "max 100000",
                "memory_max": "max",
                "cpuset_cpus": "0-7",
            },
            "load_avg": [6.21, 5.88, 4.97],
        },
    )
    builder.add_json(
        layout.META_TARGET,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "pid": pid,
            "comm": DEFAULT_COMM,
            "cmdline": [f"/usr/local/bin/{DEFAULT_COMM}", "--config", "/etc/app.conf"],
            "exe": f"/usr/local/bin/{DEFAULT_COMM}",
            "cwd": "/srv/app",
            "start_time_ticks": 1_884_231,
            "thread_count_start": thread_count,
            "thread_count_end": end_count,
            "died_during_run": degraded,
            "cpu_before": {
                "window_s": 5.0,
                "utime_ticks": 1_204_331,
                "stime_ticks": 402_118,
                "cpu_pct": 172.4,
            },
            "cpu_after": {
                "window_s": 5.0,
                "utime_ticks": 1_205_402,
                "stime_ticks": 402_622,
                "cpu_pct": 183.1,
            },
            "maps_summary": {
                "libc": "/usr/lib/x86_64-linux-gnu/libc.so.6",
                "main_exe_base": "0x555555554000",
            },
        },
    )
    builder.add_json(
        layout.META_THREADS,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "sampled_at_start": None,
            "sampled_at_end": None,
            "clk_tck": 100,
            "threads": threads_doc,
        },
    )

    # -- stacks --------------------------------------------------------
    # Enough of a boost to push the unknown frame ratio past the 30% line the
    # viewer red flags, which is the whole point of --bad-frame-pointers.
    # Spread across the roster rather than a handful of lines: a profile of a
    # 315 thread process holds thousands of distinct stacks, and a viewer that
    # has only ever been shown eight has not been shown the problem it exists
    # to solve.
    oncpu = _spread_over_threads(
        seed,
        "oncpu",
        load,
        _apply_load(_ONCPU_STACKS, scenario, "oncpu"),
        roster,
        bad_frame_pointers,
    )
    builder.add_folded(layout.STACK_ONCPU, oncpu)
    builder.add_folded(
        layout.STACK_OFFCPU,
        _spread_over_threads(
            seed,
            "offcpu",
            load,
            _apply_load(_OFFCPU_STACKS, scenario, "offcpu"),
            roster,
            bad_frame_pointers,
        ),
    )
    builder.add_folded(
        layout.STACK_FUTEX,
        _spread_over_threads(
            seed,
            "futex",
            load,
            _apply_load(_FUTEX_STACKS, scenario, "futex"),
            roster,
            bad_frame_pointers,
        ),
    )

    # -- histograms and tables ----------------------------------------
    builder.add_json(
        layout.HIST_RUNQLAT,
        _histogram_doc(
            "runqlat",
            "us",
            "runqlat.bt:@runq_us",
            [
                _series("", _pow2_buckets(rng, peak_us=8, total=1_842_000, high_tail=False)),
                _series(
                    "TimerWheel0",
                    _pow2_buckets(rng, peak_us=32, total=91_000, high_tail=True),
                ),
            ],
        ),
    )
    builder.add_json(
        layout.HIST_OFFCPU_DURATION,
        _histogram_doc(
            "offcpu_duration",
            "us",
            "offcpu.bt:@offcpu_hist",
            [_series("", _pow2_buckets(rng, peak_us=256, total=412_000, high_tail=True))],
        ),
    )

    futex_rows: List[List[Any]] = []
    addresses = [HOT_FUTEX_ADDR] + [
        HOT_FUTEX_ADDR + rng.randint(0x100, 0x80000) for _ in range(11)
    ]
    # Futex wait is summed over threads, so a bottleneck's total is measured
    # against the thread-seconds the run had available, not against the wall
    # clock.  Scaling it that way is not decoration: a fixed 6.8 s of wait
    # spread over 315 threads for a minute is 0.2% of each thread's time --
    # noise, not contention -- and a synthetic "hot lock" that is really noise
    # would let a bad threshold in the analysis pass its own test.
    thread_seconds = max(1.0, thread_count * float(duration_s))
    hot_share = {"heavy": 0.55, "spread": 0.02}.get(load, 0.38)
    for index, address in enumerate(addresses):
        # One address dominates on purpose: that is the TimerWheel lock the
        # whole project exists to find -- except under `spread`, where nothing
        # dominates and the tool has to say so.
        if index == 0:
            total_us = int(thread_seconds * hot_share * 1e6)
            # ~4 ms of waiting per acquisition, which is what a 50 us critical
            # section looks like once a few hundred threads are queued on it.
            count = max(1, int(total_us / 4_000))
        else:
            spread = 0.015 if load == "spread" else 0.012
            total_us = int(thread_seconds * rng.uniform(0.0004, spread) * 1e6)
            count = rng.randint(200, 9_000)
        futex_rows.append(
            [
                f"0x{address:x}",
                total_us,
                count,
                round(total_us / count, 2),
                _FUTEX_STACKS[0][0] if index == 0 else _FUTEX_STACKS[2][0],
            ]
        )
    futex_rows.sort(key=lambda row: row[1], reverse=True)

    # The same wait time, split by the call path that waited.  The hot address
    # is taken from two places and one of them is responsible for most of it,
    # which is the shape a real single-lock bottleneck has -- and the reason
    # the probe pays for the combined map at all.
    site_rows: List[List[Any]] = []
    for index, address in enumerate(addresses):
        if index == 0:
            splits = ((_FUTEX_STACKS[0][0], 0.86), (_FUTEX_STACKS[1][0], 0.14))
        else:
            splits = ((_FUTEX_STACKS[2][0], 1.0),)
        total_us = futex_rows[
            next(i for i, row in enumerate(futex_rows) if row[0] == f"0x{address:x}")
        ][1]
        calls = futex_rows[
            next(i for i, row in enumerate(futex_rows) if row[0] == f"0x{address:x}")
        ][2]
        for stack, share in splits:
            site_us = int(total_us * share)
            site_calls = max(1, int(calls * share))
            site_rows.append(
                [
                    f"0x{address:x}",
                    # The thread name frame belongs to the flame graph, not to
                    # a lock attribution: which thread took it is a different
                    # question from which code did.
                    stack.split(";", 1)[1],
                    site_us,
                    site_calls,
                    round(site_us / site_calls, 2),
                ]
            )
    site_rows.sort(key=lambda row: row[2], reverse=True)
    builder.add_json(
        layout.HIST_FUTEX_SITES,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "kind": "table",
            "name": "futex_sites",
            "source": "futex.bt:@futex_site",
            "columns": [
                {"id": "addr", "label": "uaddr", "type": "hex", "unit": "none"},
                {"id": "stack", "label": "call path", "type": "stack"},
                {"id": "total_us", "label": "total wait", "type": "int", "unit": "us", "sort": "desc"},
                {"id": "calls", "label": "calls", "type": "int", "unit": "count"},
                {"id": "avg_us", "label": "avg wait", "type": "float", "unit": "us"},
            ],
            "rows": site_rows,
            "truncated": False,
            "total_rows": len(site_rows),
        },
    )

    builder.add_json(
        layout.HIST_FUTEX_BY_ADDR,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "kind": "table",
            "name": "futex_by_addr",
            "source": "futex.bt:@futex_by_addr",
            "columns": [
                {"id": "addr", "label": "uaddr", "type": "hex", "unit": "none"},
                {"id": "total_us", "label": "total wait", "type": "int", "unit": "us", "sort": "desc"},
                {"id": "count", "label": "calls", "type": "int", "unit": "count"},
                {"id": "avg_us", "label": "avg wait", "type": "float", "unit": "us"},
                {"id": "top_stack", "label": "hottest stack", "type": "stack"},
            ],
            "rows": futex_rows,
            "truncated": False,
            "total_rows": len(futex_rows),
        },
    )

    syscalls = [
        ("futex", 202, 7_912_400, 128_400),
        ("epoll_wait", 232, 3_204_100, 18_900),
        ("clock_nanosleep", 230, 2_410_800, 61_200),
        ("read", 0, 184_300, 44_100),
        ("write", 1, 96_700, 39_800),
        ("timerfd_settime", 286, 41_200, 121_400),
    ]
    # In the degraded scenario the syscall_lat probe failed its smoke test, so
    # its output file must genuinely be absent -- a failed probe that still
    # leaves data behind would hide the bug it is meant to expose.
    syscall_table = {
        "schema_version": layout.SCHEMA_VERSION,
        "kind": "table",
        "name": "syscall_latency",
        "source": "syscall_lat.bt:@sc_total_us",
        "columns": [
            {"id": "syscall", "label": "syscall", "type": "string"},
            {"id": "nr", "label": "nr", "type": "int", "unit": "none"},
            {"id": "total_us", "label": "total", "type": "int", "unit": "us", "sort": "desc"},
            {"id": "count", "label": "calls", "type": "int", "unit": "count"},
            {"id": "avg_us", "label": "avg", "type": "float", "unit": "us"},
        ],
        "rows": [
            [name, nr, total, count, round(total / count, 2)]
            for name, nr, total, count in syscalls
        ],
        "total_rows": len(syscalls),
    }
    if not degraded:
        builder.add_json(layout.HIST_SYSCALL_LATENCY, syscall_table)

    # -- series --------------------------------------------------------
    # The series stop when the target dies; they are sampled from /proc, so
    # there is nothing left to read afterwards.
    collected_s = duration_s if not degraded else duration_s // 2 + 1
    thread_rows = []
    schedstat_rows = []
    run_ns = 0
    wait_ns = 0
    for second in range(collected_s):
        live = thread_count + rng.randint(-2, 3)
        thread_rows.append(
            [second, live, rng.randint(9_000, 14_000), rng.randint(400, 1_800)]
        )
        run_ns += rng.randint(1_400_000_000, 1_900_000_000)
        wait_ns += rng.randint(40_000_000, 220_000_000)
        schedstat_rows.append([second, run_ns, wait_ns, rng.randint(120_000, 190_000)])
    builder.add_series(layout.SERIES_THREADS, thread_rows)
    builder.add_series(layout.SERIES_SCHEDSTAT, schedstat_rows)

    # -- wakeup graph --------------------------------------------------
    hub_tid = roster[1][0]
    edges: List[Dict[str, Any]] = []
    for thread_id, _name in roster[2 : min(len(roster), 80)]:
        edges.append(
            {
                "from_tid": hub_tid,
                "to_tid": thread_id,
                "count": rng.randint(300, 5_200),
                "total_us": float(rng.randint(1_000, 90_000)),
            }
        )
        if rng.random() < 0.4:
            edges.append(
                {
                    "from_tid": thread_id,
                    "to_tid": hub_tid,
                    "count": rng.randint(20, 400),
                    "total_us": float(rng.randint(100, 9_000)),
                }
            )
    builder.add_json(
        layout.GRAPH_WAKEUP_EDGES,
        {
            "schema_version": layout.SCHEMA_VERSION,
            "source": "wakeup.bt:@wake_cnt",
            "nodes": [
                {"tid": thread_id, "name": name}
                for thread_id, name in roster[: min(len(roster), 80)]
            ],
            "edges": edges,
            "truncated": len(roster) > 80,
            "total_edges": len(edges),
        },
    )

    # -- probe stderr logs --------------------------------------------
    probes: List[ProbeResult] = [
        ProbeResult(
            name="oncpu",
            status="ok",
            outputs=[layout.STACK_ONCPU],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
            thresholds={"hz": 99},
        ),
        ProbeResult(
            name="offcpu",
            status="ok",
            outputs=[layout.STACK_OFFCPU, layout.HIST_OFFCPU_DURATION],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
            thresholds={"min_us": 100},
        ),
        ProbeResult(
            name="runqlat",
            status="ok",
            outputs=[layout.HIST_RUNQLAT],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
        ),
        ProbeResult(
            name="futex",
            status="ok",
            outputs=[
                layout.STACK_FUTEX,
                layout.HIST_FUTEX_BY_ADDR,
                layout.HIST_FUTEX_SITES,
            ],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
            thresholds={"min_us": 50},
        ),
        ProbeResult(
            name="wakeup",
            status="ok",
            outputs=[layout.GRAPH_WAKEUP_EDGES],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
        ),
        ProbeResult(
            name="syscall_lat",
            status="ok",
            outputs=[layout.HIST_SYSCALL_LATENCY],
            duration_s=float(duration_s),
            exit_reason="sigint",
            exit_code=0,
        ),
    ]

    if pmu == "basic":
        event_names = [name for group in pmu_mod.EVENT_GROUPS for name in group]
        totals: Dict[str, Dict[str, Any]] = {}
        pmu_threads: List[Dict[str, Any]] = []
        enabled = duration_s * 1_000_000_000
        multiplier = 1.8 if load == "heavy" else 1.0
        for thread_id, name in roster:
            cycles = int(rng.randint(20_000_000, 80_000_000) * multiplier)
            values = {
                "cycles": cycles,
                "instructions": int(cycles * (0.72 if load == "heavy" else 1.15)),
                "branches": cycles // 6,
                "branch_misses": cycles // (40 if load == "heavy" else 80),
                "cache_references": cycles // 5,
                "cache_misses": cycles // (19 if load == "heavy" else 45),
            }
            events = {}
            for event in event_names:
                count = values[event]
                events[event] = {
                    "raw": count, "scaled": float(count),
                    "time_enabled_ns": enabled, "time_running_ns": enabled,
                }
                total = totals.setdefault(event, {
                    "raw": 0, "scaled": 0.0,
                    "time_enabled_ns": 0, "time_running_ns": 0,
                })
                total["raw"] += count
                total["scaled"] += count
                total["time_enabled_ns"] += enabled
                total["time_running_ns"] += enabled
            pmu_threads.append({
                "tid": thread_id, "name": name, "start_time_ticks": thread_id,
                "coverage_s": float(duration_s), "events": events,
            })
        builder.add_json(layout.PMU_COUNTERS, {
            "schema_version": layout.SCHEMA_VERSION,
            "kind": "pmu", "mode": "basic", "source": "perf_event_open",
            "scope": "user", "status": "ok", "cpu_model": "Synthetic CPU",
            "arch": "x86_64", "window_s": float(duration_s),
            "thread_count_start": len(roster), "threads_measured": len(roster),
            "events": event_names, "totals": totals, "threads": pmu_threads,
            "warnings": [],
        })
        probes.append(ProbeResult(
            name="pmu_basic", status="ok", duration_s=float(duration_s),
            outputs=[layout.PMU_COUNTERS],
        ))
    elif pmu != "off":
        raise PerformerError(f"unknown PMU mode {pmu!r}")

    target_died_at = None
    if degraded:
        for probe in probes:
            if probe.name == "futex":
                probe.status = "partial"
                probe.events_lost = 12_043
                probe.warnings = ["map full: raise BPFTRACE_MAP_KEYS_MAX"]
            if probe.name == "syscall_lat":
                probe.status = "failed"
                probe.warnings = ["smoke test produced no output on this kernel"]
                probe.exit_reason = "startup_error"
                probe.exit_code = 1
                probe.outputs = []
        probes.append(
            ProbeResult(
                name="timers",
                status="skipped",
                warnings=["not part of the 'standard' profile"],
            )
        )
        target_died_at = started + _dt.timedelta(seconds=duration_s // 2 + 1)

    for probe in probes:
        if probe.status == "skipped" or probe.name == "pmu_basic":
            continue
        lines = [f"Attaching {rng.randint(2, 9)} probes..."]
        for warning in probe.warnings:
            lines.append(f"WARNING: {warning}")
        if probe.status == "failed":
            lines.append("ERROR: No probes to attach")
        builder.add_text(
            f"{layout.DIR_RAW}/{probe.name}.stderr.log", "\n".join(lines)
        )

    # -- manifest ------------------------------------------------------
    # Counted the same way the real collector counts it (parse.stacks.
    # FoldStats): frames, weighted by how often the stack was sampled, so one
    # broken stack seen a thousand times outweighs a good one seen once.
    total_frames = 0
    unknown_frames = 0
    for stack, value in oncpu:
        frames = stack.split(";")
        total_frames += value * len(frames)
        unknown_frames += value * sum(1 for frame in frames if frame == "[unknown]")
    ratio = round(unknown_frames / total_frames, 4) if total_frames else 0.0

    document = build_manifest(
        label=label,
        profile=profile,
        duration_s=float(duration_s),
        started_at=started,
        ended_at=started + _dt.timedelta(seconds=duration_s),
        actual_duration_s=float(duration_s if not degraded else duration_s // 2 + 1),
        target=TargetInfo(
            pid=pid,
            comm=DEFAULT_COMM,
            thread_count_start=thread_count,
            thread_count_end=end_count,
            cmdline=[f"/usr/local/bin/{DEFAULT_COMM}", "--config", "/etc/app.conf"],
            exe=f"/usr/local/bin/{DEFAULT_COMM}",
        ),
        probes=probes,
        quality=Quality(
            frame_pointers_ok=not bad_frame_pointers,
            unknown_frame_ratio=ratio,
            estimated_overhead_pct=6.2 if not degraded else 14.8,
            unknown_frame_samples=unknown_frames,
            total_frame_samples=total_frames,
            overhead={
                "cpu_pct_before": 172.4,
                "cpu_pct_after": 183.1,
                "sample_window_s": 5.0,
            },
            notes=(
                ["frame pointer check failed; stacks are not trustworthy"]
                if bad_frame_pointers
                else []
            ),
        ),
        tool_versions={
            **provenance.tool_versions(),
            "performer": _version(),
            "bpftrace": "0.20.2",
            "kernel": "5.15.0-91-generic",
            "python": "3.10.12",
            "distro": "Ubuntu 22.04.3 LTS",
        },
        tags=list(tags),
        notes=notes
        or (
            "Synthetic bundle produced by 'performer fake-run'. Not real data. "
            f"Load scenario: {load} -- {scenario.description}."
        ),
        target_died_at=target_died_at,
        warnings=(
            ["target process exited before the requested duration elapsed"]
            if degraded
            else []
        ),
    )
    builder.write_manifest(document)

    if pack:
        return builder.root, builder.pack()
    return builder.root, builder.root


def _version() -> str:
    from . import __version__

    return __version__
