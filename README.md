# Performer

Reproducible runtime-behaviour capture for heavily threaded Linux processes,
plus a viewer that compares runs against each other.

`perf` plus a shell script gives you a one-shot answer. Performer gives you a
**run bundle**: a self-contained, self-describing archive of one measurement,
with enough metadata to know whether the numbers can be trusted — and a viewer
that diffs two of them so "before and after the fix" is one click rather than
an afternoon of squinting.

The immediate target is a ~315-thread C++ application whose suspected
bottlenecks are timer-manager mutex contention, glibc malloc arena contention,
and scheduler pressure from thread over-provisioning. Nothing in the tool is
specific to it.

## Architecture

```
┌──────────────────────────┐               ┌───────────────────────────┐
│  TARGET MACHINE (root)   │    .tgz       │  ANALYSIS MACHINE         │
│                          │   bundle      │  (unprivileged, browser)  │
│  performer collect  ─────────────────────▶  performer viewer         │
│  Python 3 stdlib only    │               │  static React app         │
└──────────────────────────┘               └───────────────────────────┘
```

The two halves never share a runtime. The collector must run on a locked-down
lab box, so it uses **nothing outside the Python standard library** — no pip,
no virtualenv, no network. The viewer is a static bundle that works from
`file://`, so it needs no server and parses bundles entirely client-side.

## Status: M1 complete

M0 fixed the contract — the bundle layout and the manifest schema. M1 makes it
real: preflight, one probe (`oncpu.bt`), and an actual measurement of an actual
process, end to end.

| Milestone | Scope | State |
|---|---|---|
| **M0** | Repo skeleton, bundle format, `manifest.schema.json`, `inspect`/`validate`/`fake-run` | **done** |
| **M1** | `preflight.py`, `collect` with `oncpu.bt`, folded stacks, real bundles | **done** |
| M2 | Full probe set, YAML profiles, histogram parsers | todo |
| M3 | Viewer: bundle loading, Overview, Flame, Threads | todo |
| M4 | Diff screen, differential flame graph, normalisation | todo |
| M5 | Locks table, wakeup graph, automatic verdict sentence | todo |
| M6 | localhost daemon, click-to-collect | todo |

`diff` and `daemon` are registered as commands today and exit with code 3 and
an explicit "not implemented in this milestone" message. They never pretend to
have worked. The `light`/`standard`/`deep` profiles are refused by name until
M2 defines them, rather than silently running something else.

## Collecting

On the target machine, as root, with bpftrace installed (output below is
illustrative — the numbers come from a real run's shape, not a specific one):

```console
$ ./collector/bin/performer collect --pid 205852 --duration 30 \
      --label baseline --tag before-timer-fix --out ./runs
preflight: pid 205852, profile 'oncpu'
preflight
  [ok] privileges             sufficient privileges (root)
  [ok] bpftrace               bpftrace 0.20.2 at /usr/bin/bpftrace
  [ok] target                 pid 205852 is 'Hisar_Seri_Uret' with 316 threads
  [ok] nofile                 RLIMIT_NOFILE soft limit 20000 covers the estimated need (5056)
  [ok] perf_event_paranoid    kernel.perf_event_paranoid = 2
  [ok] frame_pointers         stacks resolve (0.4% unknown frames over 2946 samples)
  [ok] smoke:oncpu            probe 'oncpu' attached and produced output
run 20260806T045451Z-baseline
  probe 'oncpu' attached (pid 26957)
stopping probes after 30.0s (duration)
  stacks/oncpu.folded: 1841 stacks, 291043 samples, 0.4% unknown frames

status:        ok
```

Preflight refuses to start a measurement that cannot produce a usable answer.
Above 30% unresolved frames it stops and tells you to rebuild with
`-fno-omit-frame-pointer`; `--ignore-quality` overrides that and marks the
bundle accordingly. A probe that fails its two-second smoke test is recorded as
`failed`, never skipped quietly.

The resulting `stacks/oncpu.folded` feeds FlameGraph unchanged:

```console
$ tar xzf runs/oxfscope-*-baseline.tgz
$ flamegraph.pl run_*/stacks/oncpu.folded > baseline.svg
```

Other collection modes:

```console
$ performer collect --pid 205852 --until-exit --label full-run   # until the target exits
$ performer preflight --pid 205852                               # just the checks
```

## Quick start without a target

No installation, no bpftrace, no root:

```console
$ ./collector/bin/performer fake-run --out ./runs --label baseline
run directory: runs/run_20260806T142530Z_baseline
bundle:        runs/oxfscope-20260806T142530Z-baseline.tgz

$ ./collector/bin/performer inspect runs/oxfscope-20260806T142530Z-baseline.tgz
```

```
run 20260806T142530Z-baseline  [ok]

  label               baseline
  profile             standard
  started / duration  2026-08-06T14:25:30Z  60 s
  target              pid 205852  Hisar_Seri_Uret  threads 315 -> 315
  tooling             bpftrace 0.20.2  |  kernel 5.15.0-91-generic  |  oxfscope 0.1.0

probes
  oncpu        ok
  offcpu       ok
  futex        ok
  ...

quality
  frame pointers      ok
  unknown frames      0.9%
  est. overhead       6.2%

flags
  none -- this run looks trustworthy

schema: OK (11 file(s) validated against ./schema)
```

`fake-run` writes a synthetic bundle: invented numbers with the shape of a real
measurement (315 threads, one dominant futex address, a timer thread at the
centre of the wakeup graph). It exists so the viewer and the diff algorithm can
be built before the collector can attach to anything, and so the layout is
proven writable and readable on a laptop with no bpftrace and no root.

To see how a bad run is reported:

```console
$ ./collector/bin/performer fake-run --out ./runs --label broken \
      --degraded --bad-frame-pointers
$ ./collector/bin/performer inspect --strict ./runs/oxfscope-*-broken.tgz
...
flags
  [X] 39% of sampled frames are [unknown]; flame graphs are misleading
      -> Rebuild the target with -fno-omit-frame-pointer, or collect with DWARF unwinding.
  [X] probe 'syscall_lat' produced nothing
  [!] target process exited during the run at 2026-08-06T14:26:01Z
  [!] probe 'futex' lost 12,043 events; its totals are a lower bound
```

`inspect` exits 0 for a readable bundle regardless of flags; `--strict` makes
an error-level flag exit 1, which is what a future CI gate would use.

## Commands

| Command | Purpose |
|---|---|
| `collect --pid N --label L` | Measure a running process into a bundle. `--duration`/`--until-exit`, `--profile`, `--tag`, `--ignore-quality`, `--force`, `--overhead-window`, `--keep-raw-stdout`, `--annotate-kernel` |
| `preflight --pid N` | Run the environment checks without collecting. `--skip-trials`, `--json` |
| `inspect <bundle>...` | Manifest summary, quality flags, schema check. `--json`, `--strict`, `--verbose`, `--no-validate` |
| `validate <bundle>...` | Schema + cross-field validation only, exit 1 on failure. `--verify-hashes` |
| `fake-run` | Write a synthetic bundle. `--degraded`, `--bad-frame-pointers`, `--threads`, `--seed`, `--no-pack` |
| `schema [name]` | Show or print the schemas this build enforces |
| `diff` / `daemon` | Registered, not implemented (exit 3) |

Bundles are accepted as either a `.tgz` or an unpacked run directory.

The CLI answers to both `oxfscope` and `performer` (`collector/bin/performer`
is a symlink); the source document uses both names interchangeably.

## Bundle format

Full contract: [`docs/bundle-format.md`](docs/bundle-format.md). In brief:

```
run_20260806T142530Z_baseline/
├── manifest.json        # the contract; validated before it is ever written
├── meta/                # system.json, target.json, threads.json
├── stacks/              # *.folded — FlameGraph format
├── hist/                # normalised histograms and aggregation tables
├── series/              # 1 Hz CSV time series
├── graph/               # wakeup_edges.json
└── raw/                 # <probe>.stderr.log — never /dev/null
```

Machine-readable schemas live in [`schema/`](schema/) and are enforced by a
~350-line JSON Schema subset validator in the collector
(`oxfscope/jsonschema.py`) — the standard-library rule rules out `jsonschema`.
An unsupported schema keyword raises rather than being skipped, so the contract
cannot quietly stop being enforced.

### The `quality` block matters most

Every manifest carries `quality.frame_pointers_ok`,
`quality.unknown_frame_ratio` and `quality.estimated_overhead_pct`. Above a 30%
unknown-frame ratio the flame graphs are actively misleading, and both the CLI
and (from M3) the viewer red-flag the run rather than drawing a pretty picture
over unusable data. Thresholds live in one place, `oxfscope/report.py:Thresholds`,
so the CLI and the viewer cannot drift apart.

### Untrusted by construction

Bundles are produced on one machine and opened on another, so reading one is
treated as parsing untrusted input:

* archives are **never extracted** — members are streamed out of the tarball;
* link, device and fifo members are rejected, as are `..` components, absolute
  paths, and archives with more than one top-level directory;
* per-member size is capped;
* `--verify-hashes` re-checks every recorded `sha256`, and files present but
  absent from `manifest.files` (or vice versa) are reported.

## Repository layout

```
collector/
  bin/oxfscope          entry point (+ performer symlink); no install needed
  oxfscope/
    layout.py           every bundle-relative path, named once
    jsonschema.py       stdlib-only JSON Schema subset validator
    manifest.py         manifest assembly and cross-field rules
    bundle.py           build, pack and (defensively) read bundles
    proc.py             /proc readers: threads, schedstat, CPU, cgroups
    preflight.py        the seven environment checks
    profiles.py         probe sets and overhead tiers
    runner.py           probe supervision, signals, watcher, 1 Hz sampler
    parse/stacks.py     bpftrace stack maps -> FlameGraph folded
    collect.py          the measurement itself
    report.py           inspect rendering and quality thresholds
    fake.py, cli.py
  profiles/             M2: light/standard/deep YAML definitions
probes/oncpu.bt         on-CPU sampling; the rest arrive in M2
schema/                 JSON Schemas — the machine-readable contract
viewer/                 M3: Vite + React + TypeScript, built to viewer/dist
docs/bundle-format.md   the human-readable contract
tests/target/           a C++ workload with a known bottleneck
tests/                  stdlib unittest, no test dependencies
```

## Tests

```console
$ make check         # builds the C++ target, then runs everything
$ make test          # or: python3 -m unittest discover -s tests -t .
```

193 tests, no test dependencies, about 80 seconds. What they actually exercise:

* **Parsing** against captured bpftrace output from two release generations
  (`tests/fixtures/bpftrace/`), including the cases that break naive parsers:
  C++ symbols containing commas, `operator+`, hex offsets, module suffixes,
  unresolved addresses, empty stacks and truncated output.
* **The contract**: schema validator, manifest cross-field rules
  (`tests/fixtures/manifest/valid_*.json` and `invalid_*.json`, each of the
  latter carrying a `_why_invalid` note), bundle round-trips, hostile archives.
* **A real process**: `/proc` readers, thread inventory and series run against
  `tests/target/contention`, a C++ program with 8–315 real threads.
* **Real process supervision**: SIGINT/SIGTERM/SIGKILL escalation, process
  groups, target death, pid recycling — driven by `tests/fake_bpftrace.py`, a
  test double that reproduces the behaviours the collector depends on, most
  importantly that a probe writes its maps *only* when SIGINTed.

There is no bpftrace in CI, so the eBPF layer is the one thing stubbed. Every
other stage — process groups, signals, `/proc`, parsing, bundle writing,
validation — runs for real.

## Deviations from the source specification

Recorded deliberately; each one is a place the spec could not be followed
literally.

1. **`collector/bin/oxfscope` instead of `collector/oxfscope`.** The spec puts
   an executable file and a package directory at the same path, which no
   filesystem allows. The package keeps its name; the executable moved into
   `bin/`.
2. **`manifest.status` is required.** The spec's example manifest has no
   run-level status, but §6.2 requires a bundle to be markable as `partial`.
   It is derived from the probe outcomes (`derive_status`), and a manifest
   claiming `ok` while a probe failed is rejected as invalid.
3. **`manifest.files` added.** An inventory with sizes and hashes. The viewer
   needs to know which optional artifacts exist (`offwake.folded` "if present")
   without probing, and it makes corruption detectable.
4. **Payload schemas beyond the manifest.** The spec names only
   `manifest.schema.json`, but "fix the bundle format first" is hollow if
   `hist/*.json` and `graph/wakeup_edges.json` have no defined shape. Files
   under `hist/` carry a `kind` discriminator (`histogram` or `table`) because
   `futex_by_addr.json` is a table, not a histogram.
5. **`label` character set restricted** to `[A-Za-z0-9._-]`. Labels reach file
   names and, at M6, a daemon API; validating at the type level is cheaper than
   remembering to escape.
6. **Overhead is measured during the run, not just around it.** §5 asks for a
   5-second CPU sample immediately before and immediately after. Both of those
   measure the *untraced* target, so their difference is roughly zero whatever
   the tracing cost. The before/after samples are still taken and recorded, and
   the estimate compares them against the target's CPU over the traced window
   itself — which is where the cost is actually paid.
7. **M1 ships one profile, `oncpu`.** `light`/`standard`/`deep` are refused by
   name with a pointer to M2 rather than silently running a different probe
   set. M2 brings the YAML definitions — and, because of the stdlib-only rule,
   its own small YAML reader.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
