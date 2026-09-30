# PMU counters

`performer collect --pid PID --profile standard --pmu basic --label baseline`
adds hardware counts to the usual eBPF run. PMU is independent of the
`light`, `standard`, and `deep` probe profiles and is off by default. The
browser's New measurement form offers the same choice.
If the eBPF smoke tests all fail but PMU preflight succeeds, the run can still
collect PMU counters and records the failed probes in the bundle.

The first PMU mode counts user-space `cycles`, `instructions`, `branches`,
`branch_misses`, `cache_references`, and `cache_misses`. It attaches to every
existing thread and watches for new ones. Each thread's count, duration of
coverage, raw count, scaled count, and kernel `time_enabled`/`time_running`
values are saved in `pmu/counters.json`. A PMU source appears as `pmu_basic`
in `manifest.probes`, so partial PMU data affects the run status and cannot
disappear silently. The optional payload is compatible with older bundles.
New threads are checked every 200 ms, so shorter-lived threads may be missed.
Coverage is the time counters were attached, not proof that a thread ran for
the whole measurement window.

Overview shows process totals and ratios; Threads shows cycles, IPC, and
coverage per thread; Diff compares matching CPU models and normalizes totals
by run duration. A ratio is hidden when either counter ran for less than 90%
of its enabled time. Missing events and attachment errors are visible as
warnings. The generic cache miss event is **not** labelled as LLC misses.

These are counts over a thread or process, not counts assigned to a function.
An on-CPU stack beside an IPC value does not prove that function had that IPC.
The viewer does not turn low IPC into a memory-bound verdict. Such attribution
requires PMU sampling and more specific hardware metrics.

Preflight checks the required cycles/instructions group before collection.
Unsupported optional event groups are reported and the PMU result is partial.
The first implementation supports Linux x86_64 and aarch64. A container or
virtual machine that does not expose hardware counters cannot validate a live
PMU run; `fake-run --pmu basic` creates synthetic data for testing the bundle
and viewer, clearly marked as synthetic.

On a real Ubuntu host, run `sudo python3 tests/pmu_live_smoke.py` to check
actual per-thread counts. It starts a CPU worker after the counters begin,
then verifies that the worker's cycles and instructions are present. CI tries
the same check but reports a restricted virtual PMU as skipped.
