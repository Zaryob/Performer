# Bundle format (schema_version 1)

The contract between the collector and the viewer. Everything downstream —
probes, parsers, diff algorithm, UI — is written against this document and the
machine-readable schemas in [`../schema/`](../schema/). Change it here first.

## Archive

One measurement produces one archive:

```
performer-<run_id>.tgz
```

containing **exactly one** top-level directory, `run_<run_id>` with the first
hyphen replaced by an underscore:

```
performer-20260806T142530Z-baseline.tgz
  └── run_20260806T142530Z_baseline/
```

`run_id` is `<YYYYMMDD>T<HHMMSS>Z-<label>`, always UTC. The timestamp is the
start of the measurement window and must agree with `manifest.started_at`;
`label` matches `[A-Za-z0-9._-]{1,64}` because it reaches file names.

Readers must not extract archives. `performer.bundle` streams members out of the
tarball and rejects links, devices, `..` components, absolute paths, oversized
members and multi-root archives.

## Directory layout

```
run_20260806T142530Z_baseline/
├── manifest.json
├── meta/
│   ├── system.json          kernel, distro, CPU count, memory, cgroup limits
│   ├── target.json          pid, comm, cmdline, thread counts, CPU samples
│   └── threads.json         tid -> {name, start_schedstat, end_schedstat}
├── stacks/
│   ├── oncpu.folded
│   ├── offcpu.folded
│   ├── futex.folded
│   └── offwake.folded       optional
├── hist/                    histograms and aggregation tables
│   ├── runqlat.json         run queue latency, plus per-thread stats
│   ├── offcpu_duration.json blocked interval distribution
│   ├── offcpu_by_state.json blocked time split by task state
│   ├── futex_by_addr.json   wait time per lock address  <- the hot lock
│   ├── futex_sites.json     that address joined to the code that takes it
│   ├── futex_duration.json  futex wait distribution
│   ├── threadlife.json      threads created and destroyed, per second
│   ├── thread_lifetime.json how long threads lived
│   ├── timers.json          timer calls and their rate
│   └── syscall_latency.json time per syscall
├── series/
│   ├── threads.csv
│   └── schedstat.csv
├── graph/
│   └── wakeup_edges.json
└── raw/
    └── <probe>.stderr.log   every probe's stderr, verbatim
```

Rules that hold for every bundle:

* **Absent means absent.** A probe that failed leaves no output file. A file
  that exists is real data. `manifest.probes[]` explains every gap.
* **`raw/` is never suppressed.** No probe's stderr goes to `/dev/null`; that is
  how a silently mis-attached probe gets found.
* Only `manifest.json` is mandatory. A bundle whose every probe failed is still
  a valid bundle, with `status: "failed"`.
* Everything except `manifest.json` is listed in `manifest.files[]` with size
  and `sha256`.

## manifest.json

Schema: [`schema/manifest.schema.json`](../schema/manifest.schema.json).
Unknown keys are rejected (`additionalProperties: false`).

```json
{
  "schema_version": 1,
  "run_id": "20260806T142530Z-baseline",
  "label": "baseline",
  "tags": ["before-timer-fix", "8-core"],
  "notes": "Free text. Shown in the viewer.",
  "started_at": "2026-08-06T14:25:30Z",
  "ended_at": "2026-08-06T14:26:30Z",
  "duration_s": 60,
  "actual_duration_s": 60,
  "profile": "standard",
  "status": "partial",
  "target_died_at": null,
  "target": {
    "pid": 205852,
    "comm": "app",
    "cmdline": ["/usr/local/bin/app", "--config", "/etc/app.conf"],
    "exe": "/usr/local/bin/app",
    "thread_count_start": 315,
    "thread_count_end": 318
  },
  "probes": [
    { "name": "offcpu", "status": "ok", "events_lost": 0, "warnings": [],
      "exit_reason": "sigint", "thresholds": { "min_us": 100 },
      "outputs": ["stacks/offcpu.folded"] },
    { "name": "futex", "status": "partial", "events_lost": 12043,
      "warnings": ["map full"] }
  ],
  "tool_versions": {
    "bpftrace": "0.20.2", "kernel": "5.15.0-91-generic", "performer": "0.1.0"
  },
  "quality": {
    "frame_pointers_ok": true,
    "unknown_frame_ratio": 0.04,
    "estimated_overhead_pct": 6.2
  },
  "files": [
    { "path": "stacks/offcpu.folded", "bytes": 118432, "sha256": "…", "rows": 2044 }
  ]
}
```

### status

| value | meaning |
|---|---|
| `ok` | every requested probe delivered, nothing lost, target survived |
| `partial` | readable but incomplete — a probe failed, events were lost, or the target died |
| `failed` | nothing usable came out |

Derived by `manifest.derive_status()`. `skipped` probes do not count against
the run. A manifest that claims `ok` while carrying a failed probe, non-zero
`events_lost`, or `target_died_at` is **invalid**, not merely suspicious — the
viewer is entitled to trust this field.

### probes[].status

| value | meaning |
|---|---|
| `ok` | ran and produced output |
| `partial` | ran, but lost events or was killed before a clean dump |
| `failed` | smoke test or startup failed; produced nothing |
| `skipped` | not requested by the profile, or disabled by preflight |

`exit_reason: "sigkill"` deserves attention: bpftrace writes all its maps when
it receives SIGINT and at no other moment, so a SIGKILLed probe produced
nothing regardless of how long it ran.

### quality

The reason a viewer can refuse to draw a graph.

| field | meaning |
|---|---|
| `frame_pointers_ok` | preflight's verdict on whether the target has usable frame pointers |
| `unknown_frame_ratio` | fraction of sampled frames that are `[unknown]`, 0..1 |
| `estimated_overhead_pct` | percentage by which the target's CPU use rose while traced |

`estimated_overhead_pct` compares the target's CPU over the traced window
against the average of two untraced samples, one taken before the run and one
after (`quality.overhead.cpu_pct_before` / `cpu_pct_during`). Comparing before
with after would compare two untraced states and always report roughly zero.
Negative results clamp to zero — tracing cannot make the target cheaper.

Thresholds applied by `performer/report.py` — and, from M3, by the viewer:

| condition | level |
|---|---|
| `unknown_frame_ratio > 0.30` or `frame_pointers_ok == false` | **error** — rebuild with `-fno-omit-frame-pointer` |
| `unknown_frame_ratio > 0.10` | warning |
| `estimated_overhead_pct > 30` | **error** — the measurement changed the workload |
| `estimated_overhead_pct > 15` | warning |
| any `events_lost > 0` | warning — totals are a lower bound |

If `unknown_frame_samples` and `total_frame_samples` are both present they must
agree with `unknown_frame_ratio` to within 1%.

## stacks/*.folded

FlameGraph "folded" format, one stack per line, root first, `;` separated,
value last:

```
app;start_thread;WorkerThread::run();TimerWheel::arm();__lll_lock_wait 4820103
```

* `oncpu.folded` — sample counts from `profile:hz:99`
* `offcpu.folded` — microseconds blocked
* `futex.folded` — microseconds in `futex()`
* `offwake.folded` — optional, blocked stack plus waker stack

Frame ordering, which every reader depends on: **thread name first, then the
user stack root-to-leaf, then the kernel stack on top.** bpftrace prints stacks
leaf-first, so the collector reverses them.

Normalisation applied when folding (`parse/stacks.py`):

* trailing symbol offsets are stripped (`main+66` and `main+0x42` both become
  `main`), so two samples in the same function collapse into one path —
  anchored at the end of the string so `operator+` and `operator++` survive;
* a trailing module annotation (` (/usr/local/bin/app)`) is removed;
* an address bpftrace could not symbolise (`0x7f3c8a4419a1`) becomes
  `[unknown]` — named rather than hidden, because it is exactly what the
  quality block reports;
* `;` inside a symbol becomes `:`, since it separates frames in this format;
* kernel frames get a `_[k]` suffix only with `--annotate-kernel`.

Values are integers: sample counts for `oncpu`, microseconds for the others.
These files feed `flamegraph.pl` unchanged, which is how M1 is verified.

## hist/*.json

Two shapes, distinguished by `kind`.

**`kind: "histogram"`** ([`hist.schema.json`](../schema/hist.schema.json)) — a
`hist()`/`lhist()` map. Buckets are ordered and non-overlapping, `lo` inclusive
and `hi` exclusive. An open edge is `null`: the top bucket has `hi: null`, and
an `lhist` underflow bucket has `lo: null`. One file may carry several keyed
`series`.

```json
{ "schema_version": 1, "kind": "histogram", "name": "runqlat", "unit": "us",
  "source": "runqlat.bt:@runq_us",
  "series": [ { "key": "", "buckets": [ { "lo": 0, "hi": 1, "count": 12 } ],
                "total_count": 12 } ] }
```

**`kind: "table"`** ([`table.schema.json`](../schema/table.schema.json)) — a
keyed map such as futex wait time per `uaddr` or total time per syscall.
Column metadata is explicit so the viewer sorts and formats without guessing.

```json
{ "schema_version": 1, "kind": "table", "name": "futex_by_addr",
  "columns": [ { "id": "addr", "type": "hex" },
               { "id": "total_us", "type": "int", "unit": "us", "sort": "desc" } ],
  "rows": [ ["0x7f3c8a001240", 6820000] ] }
```

Rows are positional arrays matching `columns`; `null` means "not measured".
`truncated`/`total_rows` record top-N clipping.

A column of `type: "stack"` holds a folded call path — `frame;frame;frame`,
root first, the same shape as `stacks/*.folded` without the trailing value.
`futex_sites.json` is the reason the type exists:

```json
{ "schema_version": 1, "kind": "table", "name": "futex_sites",
  "columns": [ { "id": "addr", "type": "hex" },
               { "id": "stack", "type": "stack" },
               { "id": "total_us", "type": "int", "unit": "us", "sort": "desc" } ],
  "rows": [ ["0x7f3c8a001240",
             "WorkerThread::run();TimerWheel::arm();pthread_mutex_lock;__lll_lock_wait",
             6180000] ] }
```

The probe keys this map on the address **and** the stack together rather than
emitting two maps and joining them here, because that join does not exist:
knowing the hottest address and, separately, the hottest call path does not
establish that they are the same contention. An address alone is an identity
within one run; this is what turns it into a line of code.

## series/*.csv

1 Hz samples read from `/proc`. Headers are fixed and validated on read —
mis-parsed columns are worse than a missing file.

| file | header |
|---|---|
| `series/threads.csv` | `t_s,thread_count,ctxt_voluntary,ctxt_involuntary` |
| `series/schedstat.csv` | `t_s,run_ns,wait_ns,timeslices` |

`t_s` is whole seconds since `started_at`. `run_ns`/`wait_ns` are cumulative
process-wide totals. The series stop when the target dies.

## graph/wakeup_edges.json

Directed edge list from `sched:sched_wakeup`
([`wakeup_edges.schema.json`](../schema/wakeup_edges.schema.json)):

```json
{ "schema_version": 1,
  "nodes": [ { "tid": 205853, "name": "TimerWheel0" } ],
  "edges": [ { "from_tid": 205853, "to_tid": 205871, "count": 4210,
               "total_us": 88213.0 } ] }
```

`total_us` is nullable — it needs runqlat correlation, which no profile
collects yet, so today it is always null. Threads referenced only by an edge are legal; the viewer falls back to
`meta/threads.json` and then to the bare tid. Edges may point at tids outside
the target process (`nodes[].external: true`).

## meta/

* [`system.schema.json`](../schema/system.schema.json) — kernel, arch, distro,
  CPU count and model, memory, `perf_event_paranoid`, `ulimit -n`, cgroup
  limits. Enough to answer "are these two runs comparable at all?"
* [`target.schema.json`](../schema/target.schema.json) — pid, comm, cmdline,
  exe, `start_time_ticks` (so a recycled pid is not mistaken for the same
  process), thread counts, and the before/after CPU samples behind
  `estimated_overhead_pct`.
* [`threads.schema.json`](../schema/threads.schema.json) — every tid with its
  name and its `schedstat` at both ends of the run. Read from `/proc`, so it
  survives total eBPF failure and still populates the Threads table.

## Probe windows

Every probe in a run covers the same window. They are all started before any
of them is waited on, and all SIGINTed at the same instant, because otherwise
the first probe would trace seconds that the last one missed while
`duration_s` claimed a single window for all of them. `probes[].duration_s`
records each probe's own lifetime, which includes its startup and its map
dump and is therefore always a little longer than the run.

## Compatibility

`schema_version` is a single integer, present in `manifest.json` and in each
payload document. A reader that does not recognise the version must refuse the
bundle rather than guess. Additive, optional fields do not bump it; removing a
field, changing a type, or changing the meaning of a value does.
