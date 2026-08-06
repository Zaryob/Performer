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

## Status: M0 complete

M0 fixes the contract — the bundle layout and the manifest schema — before any
probe code exists, because everything downstream is written against it.

| Milestone | Scope | State |
|---|---|---|
| **M0** | Repo skeleton, bundle format, `manifest.schema.json`, `inspect`/`validate`/`fake-run` | **done** |
| M1 | `preflight.py`, single probe (`oncpu.bt`) end to end | todo |
| M2 | Full probe set, profiles, process supervision, partial bundles | todo |
| M3 | Viewer: bundle loading, Overview, Flame, Threads | todo |
| M4 | Diff screen, differential flame graph, normalisation | todo |
| M5 | Locks table, wakeup graph, automatic verdict sentence | todo |
| M6 | localhost daemon, click-to-collect | todo |

`collect`, `diff` and `daemon` are registered as commands today and exit with
code 3 and an explicit "not implemented in this milestone" message. They never
pretend to have worked.

## Quick start

No installation. Clone and run:

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
| `inspect <bundle>...` | Manifest summary, quality flags, schema check. `--json`, `--strict`, `--verbose`, `--no-validate` |
| `validate <bundle>...` | Schema + cross-field validation only, exit 1 on failure. `--verify-hashes` |
| `fake-run` | Write a synthetic bundle. `--degraded`, `--bad-frame-pointers`, `--threads`, `--seed`, `--no-pack` |
| `schema [name]` | Show or print the schemas this build enforces |
| `collect` / `diff` / `daemon` | Registered, not implemented (exit 3) |

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
  oxfscope/             the package: bundle.py, manifest.py, jsonschema.py,
                        layout.py, report.py, fake.py, cli.py
  profiles/             M2: light/standard/deep profile definitions
probes/                 M2: bpftrace programs
schema/                 JSON Schemas — the machine-readable contract
viewer/                 M3: Vite + React + TypeScript, built to viewer/dist
docs/bundle-format.md   the human-readable contract
tests/                  stdlib unittest, no test dependencies
```

## Tests

```console
$ make test          # or: python3 -m unittest discover -s tests -t .
```

84 tests, no dependencies, under a second. They cover the schema validator, the
manifest cross-field rules (via `tests/fixtures/manifest/valid_*.json` and
`invalid_*.json`, each of the latter carrying a `_why_invalid` note), bundle
build/pack/read round-trips, the hostile-archive cases above, and every CLI
exit code.

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

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
