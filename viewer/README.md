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
seconds to lay out and stutter afterwards. And the thread frame is merged away
by default: with 315 roots, a call path taken by every thread is drawn 315
times and none of the slivers is wide enough to read. Merging answers "where
does the time go"; the thread filter answers "which thread" once there is a
reason to ask.

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
| Diff | two runs joined on their call paths: differential flame graph, biggest movers, the paths that appeared and the paths that vanished |
| Locks, Wakeups, Timeline | M5 |

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
  quality.ts          the thresholds, mirroring the collector
  components/         FlameGraph (canvas), shared UI
  screens/            Runs, Overview, Flame, Threads, Diff
verify.mjs            drives a real browser over file:// and times the render
```

`dist/` is committed so the analysis machine needs no toolchain. Rebuild it
whenever `src/` changes.
