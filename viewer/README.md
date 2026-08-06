# viewer/ — static analysis UI

Empty until **M3**. Vite + React + TypeScript, d3 for the flame graphs (drawn
by hand: `d3-flame-graph` cannot express the differential mode M4 needs),
`pako` for un-gzipping bundles in the browser, Tailwind for styling.

Hard constraints:

* **No backend.** Bundles are parsed entirely client-side.
* **Must work from `file://`.** `viewer/dist/index.html` opened by double-click
  has to work, so bundles arrive by file picker or drag-and-drop — never
  `fetch()`, which `file://` forbids.
* `viewer/dist/` is committed to the repository so the analysis machine needs
  no toolchain.

Screens: Runs, Overview (with the automatic verdict sentence), Flame
(on-CPU / off-CPU / futex, with a thread-name filter and an "hide idle threads"
switch — without them a 315-thread graph is unreadable), Diff, Threads, Locks,
Wakeups, Timeline.

The viewer reads the format described in [`../docs/bundle-format.md`](../docs/bundle-format.md)
and must apply the same quality thresholds as `collector/performer/report.py`.
`performer fake-run` produces realistic input to develop against.
