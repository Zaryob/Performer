# viewer/ — static analysis UI

Vite + React + TypeScript, built to a **single self-contained `dist/index.html`**.

```console
$ npm install
$ npm run build     # -> dist/index.html
$ npm test          # unit tests for the parsing and quality logic
$ node verify.mjs ../runs/performer-*.tgz   # end to end, in a real browser
```

Then open `dist/index.html` by double clicking it. That is the whole
deployment: the analysis machine needs a browser and nothing else.

To run the same self-contained page in Docker instead, from the repository
root run `docker compose up -d --build --force-recreate viewer` (or
`make docker-viewer`). The root
Compose file only serves the web viewer. It builds `performer-viewer:local` from source
and serves it at `http://127.0.0.1:8080/` (override the host port with
`PERFORMER_VIEWER_PORT`). `make docker-viewer-image` builds without starting a
container; `make docker-viewer-down` stops it. The running image contains no
Node.js or backend; bundles still enter through the browser's file picker or
drag-and-drop.

The page header shows its supported bundle versions and a 12-character build
fingerprint. A version 2 bundle requires a page that says `bundles v1–v2`.
If a browser says it only reads version 1, reopen a newly built
`viewer/dist/index.html` (`npm run build` in `viewer/`), or recreate the Docker
container with the command above and reload the page. Building an image alone
does not replace an already running container. `performer daemon` serves
`viewer/dist/index.html` from the checkout (or `PERFORMER_VIEWER_DIST` when
set), so rebuild that file for daemon use too.

## Constraints that shaped it

**It must work from `file://`.** Not a preference — the analysis machine is
assumed to have no server, no toolchain and possibly no network. `file://`
forbids `fetch()` *and* refuses to load ES modules, so the build inlines
everything into one HTML file (`vite-plugin-singlefile`). There is no `fetch`
anywhere in the source; bundles arrive only through the file picker or
drag-and-drop.

**No backend.** Bundles are un-gzipped (`pako`) and un-tarred in the browser.
The tar reader is written here rather than pulled in, because a bundle is a
file that arrived from another machine: link members, device members, `..`
components and multi-root archives are all rejected. Python's `tarfile` writes
PAX headers by default, so those are understood too.

**A 315 thread profile has to stay readable.** Two things follow. The graph is
drawn on a canvas rather than as SVG — tens of thousands of DOM rects take
seconds to lay out and stutter afterwards. On-CPU and off-CPU roots preserve
TIDs, so same-named threads are separate. The graph shows these roots by
default; merging is an explicit option for combining shared call paths.
The inventory table lists every thread, including those without attributable
stacks. It also shows independent `/proc` CPU deltas and flags CPU activity
without an on-CPU sample. The graph reports how many TIDs have data, drawable
roots, and roots wide enough for labels. "Show this thread" focuses by TID and
clears the share cutoff, making even a small thread's graph readable.
Off-CPU probe warnings identify observed waits still open at shutdown; these
are included in stack/state totals but not in the completed-wait histogram.

**Unusable data must not be drawn as if it were fine.** A run whose stacks
could not be resolved gets a banner on the graph itself, not just a flag on
another screen. The thresholds live in `src/quality.ts` and mirror
`collector/performer/report.py`.

## Screens

| screen | state |
|---|---|
| Runs | loaded bundles, quality flags, select one |
| Overview | run summary, probe table, quality block, artifacts |
| Flame | on-CPU / off-CPU / futex, search, zoom, icicle, thread filter, hide idle |
| Threads | every thread with CPU and runqueue time, sortable; delta columns against a baseline run |
| Locks | contended mutexes ranked by wait time, each attributed to the code that takes it |
| Wakeups | force-directed wakeup graph plus the waker ranking |
| Diff | two runs joined on their call paths: differential flame graph, biggest movers, the paths that appeared and the paths that vanished |
| Collect | only when a daemon is serving this page: pick a process, pick a profile, watch the run, open the result |
| Timeline | M5 |

The Overview leads with a **verdict**: one sentence naming the bottleneck, the
numbers it rests on, and the screen that shows the working. It declines to name
one when nothing dominates, and refuses to produce one at all when the stacks
could not be resolved — the case where a confident sentence does the most harm.
The rules and thresholds are in `src/analysis.ts`.

The Diff screen expresses both runs as a share of their own total by default.
Two measurements are almost never the same length, and comparing their raw
counts is the standard way to read a diff backwards — everything in the longer
run "grew". Frames are normalised (offsets and module suffixes stripped)
before the join, so a rebuilt binary does not read as a rewrite.

## Layout

```
src/
  bundle/types.ts     the bundle format as TypeScript, mirroring schema/
  bundle/untar.ts     ustar + PAX reader, defensive
  bundle/load.ts      File -> Bundle, and the accessors
  bundle/folded.ts    folded stacks -> flame tree, filters, search
  bundle/diff.ts      two runs -> per-path deltas, differential tree, colour
  analysis.ts         lock ranking, wakeup hubs, and the verdict
  api.ts              the daemon, when one is serving this page
  quality.ts          the thresholds, mirroring the collector
  components/         FlameGraph (canvas), shared UI
  screens/            Runs, Overview, Flame, Threads, Locks, Wakeups, Diff,
                      Collect
verify.mjs            drives a real browser over file:// and times the render
verify-daemon.mjs     drives "New measurement" against a running daemon
```

`dist/` is committed so the analysis machine needs no toolchain. Rebuild it
whenever `src/` changes.
