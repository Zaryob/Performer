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

## Status: complete through M6

M0 fixed the contract, M1 made one probe real, M2 completed the collector, M3
made the data readable, M4 made two runs comparable, M5 reached a conclusion,
and M6 closes the loop: measure and look, in one place.

| Milestone | Scope | State |
|---|---|---|
| **M0** | Repo skeleton, bundle format, `manifest.schema.json`, `inspect`/`validate`/`fake-run` | **done** |
| **M1** | `preflight.py`, `collect` with `oncpu.bt`, folded stacks, real bundles | **done** |
| **M2** | Full probe set, YAML profiles, histogram/table parsers, partial bundles | **done** |
| **M3** | Viewer: bundle loading, Overview, Flame (filter + search), Threads | **done** |
| **M4** | Diff screen, differential flame graph, normalisation, `performer diff` | **done** |
| **M5** | Locks screen, wakeup graph, automatic verdict sentence | **done** |
| **M6** | localhost daemon, token auth, click-to-collect | **done** |

`daemon` is registered as a command today and exits with code 3 and an explicit
"not implemented in this milestone" message. It never pretends to have worked.

## Profiles

Collection comes in three tiers, because tracing every context switch and every
syscall of a 315-thread process perturbs it enough to invalidate the answer.

| profile | probes | expected overhead | max duration |
|---|---|---|---|
| `light` | oncpu, runqlat, threadlife + `/proc` series | < 3% | 900 s |
| `standard` (default) | + offcpu (100 µs), futex (50 µs), wakeup | 5–15% | 300 s |
| `deep` | + syscall_lat, timers | 20–60% | 60 s |

The time budget runs opposite to the overhead on purpose: the more a tier costs
the target, the less of the target's life it may spend. Exceeding a tier's
limit needs `--force`. Profiles are YAML in
[`collector/profiles/`](collector/profiles/) and are meant to be edited on the
target machine; see that directory's README for the format.

Thresholds reach the manifest as well as the probe, because they change what
the numbers mean: "no lock waited longer than 50 µs" and "no lock waits were
recorded" are very different findings.

## Collecting

On the target machine, as root, with bpftrace installed (output below is
illustrative — the numbers come from a real run's shape, not a specific one):

```console
$ ./collector/bin/performer collect --pid 205852 --duration 30 \
      --profile standard --label baseline --tag before-timer-fix --out ./runs
preflight: pid 205852, profile 'standard'
preflight
  [ok] privileges             sufficient privileges (root)
  [ok] bpftrace               bpftrace 0.20.2 at /usr/bin/bpftrace
  [ok] target                 pid 205852 is 'app' with 316 threads
  [ok] nofile                 RLIMIT_NOFILE soft limit 20000 covers the estimated need (5056)
  [ok] perf_event_paranoid    kernel.perf_event_paranoid = 2
  [ok] frame_pointers         stacks resolve (0.4% unknown frames over 2946 samples)
  [ok] smoke:oncpu            probe 'oncpu' attached and produced output
  [ok] smoke:runqlat          probe 'runqlat' attached and produced output
  ...
run 20260806T045451Z-baseline
  probe 'oncpu' attached (pid 26957)
  ...
stopping probes after 30.0s (duration)
  stacks/oncpu.folded: 1841 stacks, 291043 samples, 0.4% unknown frames
  stacks/offcpu.folded: 902 stacks, 61403118 us blocked, 0.4% unknown frames
  hist/futex_by_addr.json: 214 lock addresses, hottest holds 71% of the wait time
  graph/wakeup_edges.json: 8814 edges, busiest waker accounts for 44% of wakeups

status:        ok
```

Every probe starts before any of them is waited on, and they are all SIGINTed
at the same instant, so the window the manifest records is the window they all
actually covered — otherwise the first probe would trace seconds the last one
missed.

Preflight reports probe startup failures as warnings and collects with the
probes that passed. The bundle is marked partial and records failed probes.
If every probe fails, collection stops and prints the first probe error;
there is no measurement to save. Trial errors appear directly in the preflight
output because its temporary stderr files are removed afterward.

Preflight refuses to start a measurement with known unusable stacks.
Above 30% unresolved frames it stops and tells you to rebuild with
`-fno-omit-frame-pointer`; `--ignore-quality` overrides that and marks the
bundle accordingly. A probe that fails its two-second smoke test is recorded as
`failed`, never skipped quietly.

The resulting `stacks/oncpu.folded` feeds FlameGraph unchanged:

```console
$ tar xzf runs/performer-*-baseline.tgz
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
bundle:        runs/performer-20260806T142530Z-baseline.tgz

$ ./collector/bin/performer inspect runs/performer-20260806T142530Z-baseline.tgz
```

```
run 20260806T142530Z-baseline  [ok]

  label               baseline
  profile             standard
  started / duration  2026-08-06T14:25:30Z  60 s
  target              pid 205852  app  threads 315 -> 315
  tooling             bpftrace 0.20.2  |  kernel 5.15.0-91-generic  |  performer 0.1.0

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

## Docker Compose demo

To run Performer against the repository's mutex-contention sample in two
containers and write a validated bundle to `runs/`:

```console
$ make docker-demo
```

The target and privileged collector share the Docker host's PID namespace so
the PID observed by `/proc` matches the one emitted by kernel trace events.
For an image/output smoke test on a machine that cannot load eBPF programs,
use `make docker-fake`. Configuration knobs, kernel requirements and the
manual Compose flow are documented in
[`docs/docker-demo.md`](docs/docker-demo.md).

`fake-run` writes a synthetic bundle: invented numbers with the shape of a real
measurement (315 threads, one dominant futex address, a timer thread at the
centre of the wakeup graph). It exists so the viewer and the diff algorithm can
be built before the collector can attach to anything, and so the layout is
proven writable and readable on a laptop with no bpftrace and no root.

To see how a bad run is reported:

```console
$ ./collector/bin/performer fake-run --out ./runs --label broken \
      --degraded --bad-frame-pointers
$ ./collector/bin/performer inspect --strict ./runs/performer-*-broken.tgz
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
| `collect --pid N --label L` | Measure a running process into a bundle. `--profile light\|standard\|deep`, `--duration`/`--until-exit`, `--tag`, `--ignore-quality`, `--force`, `--overhead-window`, `--keep-raw-stdout`, `--annotate-kernel` |
| `preflight --pid N` | Run the environment checks without collecting. `--skip-trials`, `--json` |
| `inspect <bundle>...` | Manifest summary, quality flags, schema check. `--json`, `--strict`, `--verbose`, `--no-validate` |
| `validate <bundle>...` | Schema + cross-field validation only, exit 1 on failure. `--verify-hashes` |
| `diff A B` | Compare two runs: movers, new and vanished call paths, thread deltas. `--kind`, `--raw`, `--per-thread`, `--thread`, `--min-share`, `--top`, `--json` |
| `fake-run` | Write a synthetic bundle. `--load steady\|heavy`, `--degraded`, `--bad-frame-pointers`, `--threads`, `--seed`, `--no-pack` |
| `daemon` | Serve the viewer and a localhost API for starting measurements. `--port`, `--out`, `--profile` (repeatable whitelist), `--token`, `--no-viewer`, `--open` |
| `schema [name]` | Show or print the schemas this build enforces |

Bundles are accepted as either a `.tgz` or an unpacked run directory.

## Viewing

```console
$ open viewer/dist/index.html      # or just double click it
$ make docker-viewer               # alternatively, build and serve the viewer locally
```

`dist/index.html` is one self-contained file — no server, no toolchain, no
network — and bundles are read in the browser through the file picker or by
dropping them on the page. The Docker option builds that same page into
`performer-viewer:local` and serves it only on `127.0.0.1:8080`; the container
does not receive or store bundles. Building the image may need network access
to fetch its base images and npm packages, but the running viewer does not.

On the *target* machine there is a second mode, described under
[Collecting from the browser](#collecting-from-the-browser) below: the same
file, served by `performer daemon`, with a **New measurement** button that the
`file://` copy does not have.

| screen | what it answers |
|---|---|
| **Runs** | which bundles are loaded, and which of them can be trusted |
| **Overview** | what this run measured, which probes delivered, and the quality block |
| **Flame** | where the time goes — on-CPU, off-CPU or futex, with search, zoom, icicle mode, a thread filter and an idle-thread cutoff |
| **Threads** | every thread's CPU and runqueue time, sortable — read from `/proc`, so it survives total probe failure |
| **Locks** | which mutex is costing you, ranked, and — the part that matters — which code takes it |
| **Wakeups** | who wakes whom, as a graph, because a thread that wakes three hundred others is invisible in every other view |
| **Diff** | what changed between two runs: a differential flame graph, the biggest movers, and the call paths that appeared or vanished |

Two decisions the 315-thread case forced. The graph is drawn on a **canvas**,
not as SVG: tens of thousands of DOM rects take seconds to lay out and stutter
on every interaction afterwards. And the **thread frame is merged away by
default** — with 315 roots, a call path taken by every thread is drawn 315
times and none of the slivers is wide enough to read. Merging answers "where
does the time go"; the thread filter answers "which thread" once there is a
reason to ask.

A run whose stacks could not be resolved gets a banner **on the graph itself**,
not just a flag elsewhere: a flame graph over `[unknown]` frames looks exactly
like a real one, which is what makes it dangerous.

## The verdict

The Overview opens with a sentence naming the bottleneck:

> **A single mutex is the bottleneck: the one taken by `TimerWheel::arm()`.**
> · 83% of all futex wait time is on one address (`0x7f3c8a001240`)
> · 7,182 s of waiting across 1,795,500 waits — 38% of each thread's time, on average
> · 86% of that address's wait comes from `TimerWheel::arm()`
> → Locks — the call paths that take it are listed under that address.

Everything else in this tool presents evidence and lets you weigh it, which is
the right default. It is also not enough: the person who most needs the tool is
the one who does not already know that a 90% share of futex time at one address
means a single contended mutex. Three rules keep the sentence from becoming a
liability:

* **A verdict carries its evidence.** Never the claim alone — always the share,
  the address, the call path, and the screen that shows the working. A
  conclusion you cannot falsify on sight is worse than none.
* **"Nothing dominates" is a valid answer**, presented just as prominently. On a
  real profile it is the more common one, and burying it would push the reader
  into inventing a finding.
* **Unusable data produces no verdict at all.** A run whose stacks did not
  resolve still yields a confident-looking ranking, and that is precisely when
  a machine-written sentence does the most damage.

The thresholds live in `viewer/src/analysis.ts` and are deliberately high. The
cost of a false positive is someone spending a week on the wrong lock; the cost
of a false negative is that they read the screens themselves, which they were
going to do anyway.

**A lock address is not an answer.** `0x7f3c8a001240` is an identity within one
run and nothing more, so `futex.bt` keys its map on the address *and* the stack
together. That costs a second aggregation, and it is the difference between a
finding and a line of code: the two separate maps cannot be joined afterwards,
because knowing the hottest address and, separately, the hottest call path does
not establish that they are the same contention.

## Collecting from the browser

```console
# performer daemon --out /var/tmp/runs
performer daemon on http://127.0.0.1:7878/
  output directory: /var/tmp/runs
  profiles:         deep, light, standard

  token: 8Qw1v...
  open:  http://127.0.0.1:7878/?token=8Qw1v...
```

Open that link on the target machine and the viewer gains a **New
measurement** button: pick a process from a list that shows thread counts,
pick a profile, watch the collector's output stream, and the finished bundle
opens in the same page. No download folder, no second tool, no `scp`.

The button is absent — not disabled — when the page was opened as a file,
because `file://` forbids `fetch` and an API call could never have worked
there anyway.

This is the only part of Performer that listens on a socket, and it runs as
root on a machine somebody cares about. Its design is therefore mostly a list
of refusals:

| | |
|---|---|
| **Loopback only** | Binds `127.0.0.1`, and rejects any request whose `Host` header is not a loopback name — that is the DNS-rebinding defence, and the header is the only thing distinguishing a rebound request from a real one. |
| **Bearer token** | Printed once at startup, compared with `compare_digest`, never in a cookie. The page takes it out of the URL on load so it does not linger in history or a screenshot. |
| **No CORS headers, ever** | Silence is how "another origin may not read this" is said. Cross-origin `POST`s are refused outright. |
| **Profiles by name** | Chosen from a whitelist; `--profile light` restricts a box to the cheap tier. A path never comes from a request. |
| **Nothing reaches a shell** | The collector runs bpftrace through `subprocess` without `shell=True`, and labels, tags and pids are validated at the edge anyway — "it happens to be safe two layers down" is not a property that survives a refactor. |
| **Paths are checked twice** | A run id is matched against `RUN_ID_RE` *and* the resolved path is confirmed to be inside the output directory, so loosening the pattern later cannot silently become a file disclosure. |
| **One job at a time** | Two concurrent eBPF attachments is the way to make an overhead estimate meaningless. |

Cancelling a run is not a kill: it sets the same event Ctrl-C does, so the
probes are still SIGINTed in order and the bundle is still written. A
cancelled run is a short run, not a lost one.

`tests/test_daemon.py` drives a real server on a real socket for each of
these, including the four the milestone names — shell metacharacters, path
traversal, invalid pid, missing token.

## Comparing two runs

Nobody profiles a process once. The comparison is the deliverable, and it is
available both on the Diff screen and in a terminal:

```console
$ performer diff before.tgz after.tgz
```

```
A  steady60  20260806T222835Z-steady60  [ok]
B  heavy90   20260806T222836Z-heavy90   [ok]

   duration   60 s -> 90 s      profile  standard -> standard

oncpu  (17,109 -> 39,537 samples, compared as share of each run)
   60 paths compared; 19.09% grew, 19.09% shrank

          A         B      delta     rel  call path (leaf last)
      3.63%     5.30%     +1.67%    +46%  ...TimerWheel::arm();__lll_lock_wait;std::_Rb_tree_increment
      7.05%     5.45%     -1.61%    -23%  ...UserLogic::onEvent();UserLogic::compute();__memmove_avx
      0.00%     1.42%     +1.42%     new  ...EventQueue::grow();operator new;_int_malloc;arena_get2
```

Three decisions do most of the work:

* **Shares, not counts.** Both runs are divided by their own total by default.
  Comparing raw counts across a 60 s and a 90 s run is the standard way to
  read a diff backwards: everything in the longer one "grew". The corollary is
  worth stating — a path with an *unchanged* sample count is reported as
  having **fallen**, because it did.
* **New and vanished paths get their own lists.** "This call path did not
  exist before" is a stronger finding than any percentage, and it is exactly
  what a table sorted by delta buries. `--min-share` sets a noise floor, and
  what it removed is counted rather than hidden.
* **Frames are normalised before the join.** Offsets and module suffixes are
  stripped, so `_int_malloc+0x1f4` and `_int_malloc+0x2a0` are one path and a
  rebuilt binary does not read as a rewrite.

Threads are compared **by name**, not by tid: tids are not stable across runs,
so a per-tid comparison reports the whole pool destroyed and recreated every
time. Times are per second of run, for the same reason shares are used above.

`fake-run --load heavy` writes a synthetic bundle that differs from the
`steady` one the way a real process under more load would — the same seed
gives both runs the same thread roster and the same code paths, so what a diff
finds is the load rather than the random number generator.

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
(`performer/jsonschema.py`) — the standard-library rule rules out `jsonschema`.
An unsupported schema keyword raises rather than being skipped, so the contract
cannot quietly stop being enforced.

### The `quality` block matters most

Every manifest carries `quality.frame_pointers_ok`,
`quality.unknown_frame_ratio` and `quality.estimated_overhead_pct`. Above a 30%
unknown-frame ratio the flame graphs are actively misleading, and both the CLI
and (from M3) the viewer red-flag the run rather than drawing a pretty picture
over unusable data. Thresholds live in one place, `performer/report.py:Thresholds`,
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
  bin/performer          entry point (+ performer symlink); no install needed
  performer/
    layout.py           every bundle-relative path, named once
    jsonschema.py       stdlib-only JSON Schema subset validator
    manifest.py         manifest assembly and cross-field rules
    bundle.py           build, pack and (defensively) read bundles
    proc.py             /proc readers: threads, schedstat, CPU, cgroups
    preflight.py        the seven environment checks
    profiles.py         probe sets and overhead tiers
    runner.py           probe supervision, signals, watcher, 1 Hz sampler
    yamlish.py          stdlib-only YAML subset reader (for profiles)
    emit.py             probe output -> bundle files, one emitter per probe
    parse/stacks.py     bpftrace stack maps -> FlameGraph folded
    parse/hist.py       hist()/lhist()/stats() and value maps
    parse/syscalls.py   syscall number -> name
    collect.py          the measurement itself
    report.py           inspect rendering and quality thresholds
    fake.py, cli.py
  profiles/*.yaml       light / standard / deep overhead tiers
probes/*.bt             the eight probe programs
schema/                 JSON Schemas — the machine-readable contract
viewer/                 Vite + React + TypeScript, built to one HTML file
  src/bundle/           tar + gzip + folded-stack parsing, all client side
  src/quality.ts        the same thresholds the collector applies
  src/components/       canvas flame graph
  src/screens/          Runs, Overview, Flame, Threads
  verify.mjs            drives a real browser over file:// and times the render
docs/bundle-format.md   the human-readable contract
tests/target/           a C++ workload with a known bottleneck
tests/                  stdlib unittest, no test dependencies
```

## Tests

```console
$ make check         # builds the C++ target, then runs everything
$ make test          # or: python3 -m unittest discover -s tests -t .
```

283 Python tests (no test dependencies, under three minutes) plus 42 viewer
tests. What they actually exercise:

* **Parsing** against captured bpftrace output from two release generations
  (`tests/fixtures/bpftrace/`), including the cases that break naive parsers:
  C++ symbols containing commas, `operator+`, hex offsets, module suffixes,
  unresolved addresses, empty stacks and truncated output. One probe prints
  stack maps, histograms and stats into a single stream, so each parser is
  also checked for staying silent about the shapes that belong to the other.
* **The YAML reader and the profiles**, including that every profile's probes
  exist as `.bt` files and have an emitter — a probe nothing can parse would
  be collected for nothing.
* **The contract**: schema validator, manifest cross-field rules
  (`tests/fixtures/manifest/valid_*.json` and `invalid_*.json`, each of the
  latter carrying a `_why_invalid` note), bundle round-trips, hostile archives.
* **A real process**: `/proc` readers, thread inventory and series run against
  `tests/target/contention`, a C++ program with 8–315 real threads.
* **Real process supervision**: SIGINT/SIGTERM/SIGKILL escalation, process
  groups, target death, pid recycling — driven by `tests/fake_bpftrace.py`, a
  test double that reproduces the behaviours the collector depends on, most
  importantly that a probe writes its maps *only* when SIGINTed.

* **The viewer**, in two layers: unit tests for the tar reader (including PAX
  headers, link members and traversal paths), the folded-stack tree and the
  quality thresholds; and `viewer/verify.mjs`, which drives a real Chromium
  against the built `file://` page, loads real bundles through the file picker,
  and measures the render.

There is no bpftrace in CI, so the eBPF layer is the one thing stubbed. Every
other stage — process groups, signals, `/proc`, parsing, bundle writing,
validation, and the browser itself — runs for real.

## Deviations from the source specification

Recorded deliberately; each one is a place the spec could not be followed
literally.

1. **The tool is called `performer` throughout**, and the executable lives at
   `collector/bin/performer` while the package is `collector/performer/`. The
   spec put an executable file and a package directory at the same path, which
   no filesystem allows, and used a second tool name derived from the target
   application's framework. One neutral name, in one place, avoids both.
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
8. **The flame graph uses no d3.** The spec says to draw it by hand rather than
   with `d3-flame-graph`, which cannot express M4's differential mode — that
   part is honoured, and M4 collected on it: the Diff screen reuses the same
   renderer with a `colourFor` hook rather than adding a second one. But once
   the renderer is a canvas, d3 contributes nothing a few lines of arithmetic
   do not, so a quarter of a megabyte of dependency would be paid for nothing
   in a file that has to be opened offline.
9. **The differential graph is drawn at `a + b` wide, not `b` wide.**
   `difffolded.pl` takes widths from the newer run, which makes a call path
   that *vanished* zero pixels wide — often the most interesting thing that
   happened — and answers that with a second, negated graph. The additive
   basis puts both directions on one picture, and it is also the only choice
   that keeps a flame graph layout valid: a parent must be at least as wide as
   its children, `a + b` is additive and the intuitive `max(a, b)` is not.
   The conventional single-run bases are still one dropdown away.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
