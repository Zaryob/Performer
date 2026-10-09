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

## Native DWARF verification — 2026-10-10 (Istanbul)

On the same ARM64 host, a separate eight-worker C++ target was compiled with
`-O2 -g -fomit-frame-pointer -fno-optimize-sibling-calls -pthread`. A 5 s,
99 Hz `collect-perf --call-graph dwarf` recording preserved the nested
`worker → middle → leaf` chain, all nine target threads in the inventory,
and passed schema plus SHA-256 validation. The run status was `ok`; 1,965 of
19,650 sampled frame occurrences were unknown (10%). This is evidence for
that binary and host, not a promise that all stripped or JIT binaries unwind.

The capture starts disabled and waits for the perf control acknowledgement.
Only samples inside its recorded BOOTTIME window are folded. The bundle keeps
raw perf data, original addresses, DSOs, build IDs and mapping snapshots for
later resolution with matching debug symbols. This is an optional on-CPU path;
it does not supply off-CPU or lock measurements. See the
[perf record manual](https://man7.org/linux/man-pages/man1/perf-record.1.html).

Türkçe: Frame pointer içermeyen test hedefinde DWARF ile iç içe çağrı zinciri
korundu. Bu yol yalnızca on-CPU toplar; off-CPU ve kilit analizi için normal
collector kullanılır. Ham adresler ve build ID bilgisi sonradan eşleşen debug
sembolleriyle çözümleme yapmak için pakette saklanır.

## Build identity

Collector and viewer now report version `0.2.0`; bundle format remains version
2 and the viewer still accepts versions 1–2. New bundles record
`tool_versions.performer_commit` when a checkout or packaged commit is known,
and `performer_source_sha256` over collector, probe, profile and schema contents.
The content hash also identifies local edits and source archives without Git.
Installed packages retain the build commit without requiring Git on the target.

Debian filenames/versions include the source content fingerprint and Ubuntu
release. The viewer footer includes its version and a deterministic source
fingerprint that matches local and Docker builds. The Overview shows collector
identity as well, so a stale viewer or mismatched capture can be identified.

## Final integrated PMU check — 2026-10-10 (Istanbul)

A 16-worker target (17 inventoried threads including main) was recorded for
exactly 5.000 s with all six standard probes plus basic PMU counters. Every
standard probe certified the window; PMU status was `ok` with 17 measured
threads. Seven executable mappings and 194 original on-CPU stack records were
retained, including available module build IDs. On-CPU folded output contained
622 samples with 15.9% unknown frame occurrences. Schema/hash validation passed.
The probe processes lived roughly 18.7 s including attachment and map dumping;
that is not the 5 s capture duration.

The PMU enable batch ran 0.223–0.963 ms after capture start, and the disable
batch 0.117–0.834 ms after capture end. These measured ranges are displayed in
the viewer; counters are not claimed to switch atomically across all threads.

Module build IDs are now checked against the mapped device/inode. Deleted
mappings use `/proc/<pid>/map_files`; an inaccessible or replaced binary remains
unknown. Native perf loss counts come from recorded `LOST` / `LOST_SAMPLES`
events. Unsupported/truncated framing remains unknown, not zero, and marks the
capture partial. Layout references:
[perf file header](https://github.com/torvalds/linux/blob/v6.12/tools/perf/util/header.h),
[perf record definitions](https://github.com/torvalds/linux/blob/v6.12/tools/lib/perf/include/perf/event.h).
