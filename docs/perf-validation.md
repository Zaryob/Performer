# Comparing Performer with perf

Use time-based `cpu-clock` sampling at the same frequency and record the PID,
unwinder and clock. The two sampling timers are independent; equal frequency
does not imply equal sample counts. `cpu-clock` cannot validate off-CPU waits.

## Reproduce

On Linux with `perf`, `bpftrace`, Python and the compiled test target:

```sh
make target
sudo python3 tests/docker/perf-compare.py --out runs/perf-reference --duration 20
```

In a container, use `--privileged --pid=host` and mount tracefs as required by
the bpftrace release. Container-local PIDs do not match kernel trace PIDs.
If stack quality fails, `--ignore-quality` retains a clearly flagged run.
Ubuntu's perf wrapper may require the actual executable path via `--perf`.

The capture surrounds Performer with `perf record --clockid CLOCK_BOOTTIME
-e cpu-clock -F 99 --call-graph fp`. It exports nanosecond timestamps and clips
reference samples to `meta/window.json`'s half-open interval. Reanalyse:

```sh
python3 tests/perf_compare.py run.tgz perf-script.txt \
  --perf-meta perf-meta.json --json comparison.json
```

Missing PID, clock, rate or recording bounds remain unknown/mismatched;
the tool does not silently declare such inputs comparable. Unresolved perf
frames retain their address and module. `/proc` CPU deltas are independent
counters, with snapshot offsets recorded in the bundle.

Concurrent profiling affects overhead. Run Performer separately to assess
overhead; the paired capture is evidence about coverage and stack quality.

## Verified capture — 9 October 2026

Raspberry Pi, ARM64 Debian 13, kernel `6.12.47+rpt-rpi-2712`, bpftrace 0.23.2,
perf 6.12.47. This is the controlled 120-worker contention target, not a capture
of an arbitrary production application.

| Item | Performer | perf reference |
|---|---:|---:|
| Compared window | 20.000 s | same gate, clipped |
| Inventory TIDs | 121 | — |
| TIDs with on-CPU samples | 121 | 120 |
| On-CPU samples | 3,018 | 3,493 |
| Unknown sampled frame occurrences | 13.86% | 12.68% |
| TIDs with off-CPU stacks | 121 | not measured |

All 120 workers appear in both on-CPU sources. Performer sampled the main
thread once; perf did not select it in this interval. No TID appears only in
perf. The reference excludes 4,794 samples outside the gate. All six requested
eBPF probes completed, lost zero reported events and recorded 20.000 s,
despite roughly 35 s process lifetimes including attachment and map printing.
The bundle passes schema and SHA-256 validation. 111 observed open off-CPU
intervals were censored at the exact shared boundary (191,734 µs total).

The 0.14 readiness/arm/seal path was also exercised in an Ubuntu 22.04
container. Its newer Docker VM lacked readable BTF for old bpftrace, so the
test supplied the missing tracepoint integer typedef through `--include`;
this does not claim validation on Ubuntu 22.04's native kernel.
