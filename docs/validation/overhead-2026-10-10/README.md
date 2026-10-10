# Overhead benchmark — 10 October 2026

Raspberry Pi 5 (4 × Cortex-A76, 8 GiB, `ondemand` governor), Debian 13,
kernel `6.12.47+rpt-rpi-2712`, bpftrace 0.23.2, perf 6.12.47, Python 3.13.5,
root, `kernel.perf_event_paranoid = 2`. The I/O workload wrote to ext4 on NVMe
(`/dev/nvme0n1p2`), not tmpfs. Full environment: `results.json`.

```sh
make target
sudo python3 tests/docker/overhead-bench.py --out runs/overhead --rounds 7 \
  --duration 10 --settle 2 --io-dir /path/on/a/real/disk
python3 tests/overhead_bench.py runs/overhead/results.json
```

Each run starts a fresh `tests/target/contention` with 4 threads. Modes rotate
every round. Throughput and CPU per iteration come from the target's own
`CLOCK_BOOTTIME` / `CLOCK_PROCESS_CPUTIME_ID` ticks, which are the same
utime+stime counters `pidstat` reads. They are measured inside Performer's
certified 10 s gate, or in a window of the same length for baseline and perf.
The standard profile ran all six probes at 99 Hz. perf used
`perf record -e cpu-clock -F 99 --call-graph fp`. The two profilers never
ran together.

## Result (source `e9195ba3…`, commit `cd2e9eb`)

| Workload | Mode | Runs ok/failed | Throughput/s median [IQR] | Δ throughput | CPU µs/iter | Δ CPU/iter | Events lost | Performer self-estimate (missing) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| cpu | baseline | 7/0 | 297,606 [297,118–297,614] | — | 13.38 | — | — | — |
| cpu | performer | 7/0 | 296,979 [296,878–297,008] | −0.2% | 13.39 | +0.1% | 0 | 0.0% (0) |
| cpu | perf | 7/0 | 297,452 [297,281–297,480] | −0.1% | 13.38 | +0.0% | 0 | — |
| lock | baseline | 7/0 | 21,720 [21,715–21,722] | — | 48.44 | — | — | — |
| lock | performer | 7/0 | 21,308 [21,295–21,310] | −1.9% | 53.31 | +10.1% | 0 | 8.5% (0) |
| lock | perf | 7/0 | 21,682 [21,651–21,686] | −0.2% | 49.24 | +1.7% | 0 | — |
| io | baseline | 7/0 | 2,813 [2,777–2,908] | — | 18.16 | — | — | — |
| io | performer | 7/0 | 2,809 [2,769–2,840] | −0.1% | 23.18 | +27.6% | 0 | 22.0% (2) |
| io | perf | 7/0 | 2,823 [2,760–2,889] | +0.4% | 22.21 | +22.3% | 0 | — |

All 63 runs completed. Every Performer gate was certified, every probe was
`ok` and no events were lost.

- **CPU-bound:** profiling cost was within noise for both profilers.
- **One hot mutex:** the futex, off-CPU and wakeup probes fire on every
  contended handoff. They added about 10% CPU per iteration and cost 1.9% of
  throughput. perf's timer-only sampling cost 1.7% CPU and 0.2% throughput.
- **write + fdatasync:** throughput was device-bound and unchanged. CPU per
  iteration rose 28% for Performer and 22% for perf. The target uses only
  0.05 cores, so this is about 1.3 µs per syscall-heavy iteration.

The collector's own `estimated_overhead_pct` was 0.0% on every cpu run and
8.1–8.9% on every lock run, against the 10.1% measured here. For io it was
14–24% against 28%. Two io runs reported `null` because their untraced
samples genuinely differed by 27–33% at 4–6% CPU.

Outside the gate, preflight trials, probe attachment and map dumping took a
median of 38–43 s of wall time per collection. Untraced samples waited 1.0 s
before the run and 1.0–3.0 s after it for kernel probe teardown to finish
(see below).

## Before the sampling fix (`pre-fix/`, source `198c2462…`)

The first full run used the same harness and gave the same measured costs.
The self-estimate, however, was wrong:

- **cpu:** 6–16% on five of seven runs, against a measured 0%.
- **lock:** `null` on all seven runs.

The manifests showed why. The cpu target, pinned at 398%, read 239–386% in
its "untraced" samples, and the lock target fell from 103% to 16–52%. A
per-250 ms timeline of the target's CPU, alongside `top`, found the cause.
After bpftrace exits, kworker `events_unbound` threads used about 1.2 cores
for 2–3 s while the kernel freed BPF programs and maps, and the untraced
samples fell inside that teardown. Untraced samples now wait for the rest of
the machine to return to its pre-trial CPU level (commit `cfbffa4`).

## Limits

These figures are for one board, one kernel and three synthetic workloads at
4 threads. They are not a budget for every application. Thread count, lock
handoff rate and syscall rate scale the cost of the event probes. The
`ondemand` governor was left as configured.
