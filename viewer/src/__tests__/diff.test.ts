import { describe, expect, it } from "vitest";
import { parseFolded, flatten, type FlameNode } from "../bundle/folded";
import {
  buildDiffTree,
  diffColour,
  diffFolded,
  formatDelta,
  formatValue,
  leafOf,
  normaliseFrame,
  pathKey,
  type DiffNode,
} from "../bundle/diff";

/**
 * Two runs of the same process. B spends more of itself under the lock and
 * has one call path A never took; the shared `compute()` path is unchanged in
 * absolute terms, which after normalisation must read as a *fall* in share.
 * That last one is the case a naive diff gets wrong.
 */
const A = parseFolded(
  [
    "worker;run();lock();__lll_lock_wait 100",
    "worker;run();compute() 300",
    "worker;run();park() 100",
  ].join("\n"),
);
const B = parseFolded(
  [
    "worker;run();lock();__lll_lock_wait 500",
    "worker;run();compute() 300",
    "worker;run();backpressure();wait() 200",
  ].join("\n"),
);

describe("normaliseFrame", () => {
  it("strips symbol offsets so two builds join on the same path", () => {
    expect(normaliseFrame("_int_malloc+0x1f4")).toBe("_int_malloc");
    expect(normaliseFrame("main+66")).toBe("main");
  });

  it("leaves operator+ alone", () => {
    // The offset pattern is anchored at the end for exactly this reason.
    expect(normaliseFrame("Vec::operator+")).toBe("Vec::operator+");
    expect(normaliseFrame("Vec::operator++")).toBe("Vec::operator++");
  });

  it("strips a module annotation", () => {
    expect(normaliseFrame("main+66 (/usr/local/bin/app)")).toBe("main");
  });

  it("names an unresolved address rather than hiding it", () => {
    expect(normaliseFrame("0x7f3c8a001240")).toBe("[unknown]");
    expect(normaliseFrame("   ")).toBe("[unknown]");
  });

  it("joins a whole path", () => {
    expect(pathKey(["app+1", "run()+0x20"])).toBe("app;run()");
  });
});

describe("diffFolded", () => {
  it("subtracts shares, not raw counts", () => {
    const diff = diffFolded(A, B, { mergeThreads: false, minShare: 0 });
    expect(diff.rawTotalA).toBe(500);
    expect(diff.rawTotalB).toBe(1000);

    const byPath = new Map(diff.paths.map((entry) => [entry.path, entry]));
    const lock = byPath.get("worker;run();lock();__lll_lock_wait");
    expect(lock?.a).toBeCloseTo(0.2);
    expect(lock?.b).toBeCloseTo(0.5);
    expect(lock?.delta).toBeCloseTo(0.3);

    // 300 samples in both runs, but a smaller slice of the second one. A diff
    // that reported this as "unchanged" would be answering a question nobody
    // asked.
    const compute = byPath.get("worker;run();compute()");
    expect(compute?.a).toBeCloseTo(0.6);
    expect(compute?.b).toBeCloseTo(0.3);
    expect(compute?.delta).toBeCloseTo(-0.3);
  });

  it("compares raw counts when normalisation is off", () => {
    const diff = diffFolded(A, B, {
      mergeThreads: false,
      minShare: 0,
      normalise: false,
    });
    const compute = diff.paths.find((e) => e.path.endsWith("compute()"));
    expect(compute?.delta).toBe(0);
    const lock = diff.paths.find((e) => e.path.endsWith("__lll_lock_wait"));
    expect(lock?.delta).toBe(400);
  });

  it("keeps the paths that exist on only one side apart", () => {
    const diff = diffFolded(A, B, { mergeThreads: false, minShare: 0 });
    expect(diff.onlyInB.map((e) => e.path)).toEqual([
      "worker;run();backpressure();wait()",
    ]);
    expect(diff.onlyInA.map((e) => e.path)).toEqual(["worker;run();park()"]);
    // A path that did not exist has no ratio, and saying "+infinity%" or
    // "+0%" would both be lies.
    expect(diff.onlyInB[0]!.ratio).toBeNull();
  });

  it("sorts by the size of the change, not by the size of the path", () => {
    const diff = diffFolded(A, B, { mergeThreads: false, minShare: 0 });
    // The two 30-point movers come first even though `park()` and
    // `backpressure()` are the more dramatic-looking appeared/vanished pair.
    expect(diff.paths.slice(0, 2).map((entry) => entry.path).sort()).toEqual([
      "worker;run();compute()",
      "worker;run();lock();__lll_lock_wait",
    ]);
    const magnitudes = diff.paths.map((entry) => Math.abs(entry.delta));
    expect(magnitudes).toEqual([...magnitudes].sort((x, y) => y - x));
  });

  it("balances: what grew equals what shrank once normalised", () => {
    const diff = diffFolded(A, B, { mergeThreads: false, minShare: 0 });
    expect(diff.grew).toBeCloseTo(diff.shrank);
  });

  it("merges the thread frame when asked", () => {
    const perThread = parseFolded("t1;run();f() 10\nt2;run();f() 10");
    const diff = diffFolded(perThread, perThread, {
      mergeThreads: true,
      minShare: 0,
    });
    expect(diff.paths).toHaveLength(1);
    expect(diff.paths[0]!.path).toBe("run();f()");
    expect(diff.paths[0]!.delta).toBe(0);
  });

  it("filters on the thread frame even after merging it away", () => {
    const perThread = parseFolded("keepme;run();f() 10\nother;run();f() 90");
    const diff = diffFolded(perThread, perThread, {
      mergeThreads: true,
      threadFilter: "keep",
      minShare: 0,
    });
    expect(diff.rawTotalA).toBe(10);
  });

  it("counts what the noise floor removed rather than hiding it", () => {
    const noisy = parseFolded("t;big() 10000\nt;tiny() 1");
    const diff = diffFolded(noisy, noisy, {
      mergeThreads: false,
      minShare: 0.001,
    });
    expect(diff.paths).toHaveLength(1);
    expect(diff.belowThreshold).toBe(1);
  });

  it("normalises frames before joining, so a rebuild is not a rewrite", () => {
    const before = parseFolded("app+1;run()+0x20;work()+4 10");
    const after = parseFolded("app+9;run()+0x88;work()+300 20");
    const diff = diffFolded(before, after, { mergeThreads: false, minShare: 0 });
    expect(diff.paths).toHaveLength(1);
    expect(diff.onlyInA).toHaveLength(0);
    expect(diff.onlyInB).toHaveLength(0);
  });

  it("survives an empty run without dividing by zero", () => {
    const diff = diffFolded(parseFolded(""), B, {
      mergeThreads: false,
      minShare: 0,
    });
    expect(diff.totalA).toBe(0);
    expect(diff.onlyInB).toHaveLength(3);
    expect(diff.paths.every((entry) => Number.isFinite(entry.delta))).toBe(true);
  });
});

describe("buildDiffTree", () => {
  const diff = diffFolded(A, B, { mergeThreads: false, minShare: 0 });

  it("carries both runs' numbers on every node", () => {
    const root = buildDiffTree(diff, "both");
    expect(root.a).toBeCloseTo(1);
    expect(root.b).toBeCloseTo(1);

    const nodes = flatten(root) as DiffNode[];
    const lock = nodes.find((node) => node.name === "lock()");
    expect(lock?.a).toBeCloseTo(0.2);
    expect(lock?.b).toBeCloseTo(0.5);
    expect(lock?.delta).toBeCloseTo(0.3);
  });

  it("sums children into parents", () => {
    const root = buildDiffTree(diff, "both");
    const run = flatten(root).find((node) => node.name === "run()") as DiffNode;
    const childA = run.children.reduce((sum, child) => sum + child.a, 0);
    expect(run.a).toBeCloseTo(childA);
  });

  it("keeps a path that vanished visible on the default layout", () => {
    // The whole reason the default width is A + B rather than B: with widths
    // taken from the newer run, a path that disappeared is zero pixels wide.
    const both = flatten(buildDiffTree(diff, "both"));
    const after = flatten(buildDiffTree(diff, "after"));
    expect(both.some((node) => node.name === "park()")).toBe(true);
    expect(after.some((node) => node.name === "park()")).toBe(false);
    expect(after.some((node) => node.name === "wait()")).toBe(true);
  });

  it("never makes a parent narrower than its children", () => {
    // A flame graph layout is only valid if this holds; it is what forced the
    // additive basis rather than the intuitive max(a, b).
    for (const basis of ["both", "after", "before"] as const) {
      for (const node of flatten(buildDiffTree(diff, basis))) {
        const children = node.children.reduce((sum, c) => sum + c.value, 0);
        expect(node.value).toBeGreaterThanOrEqual(children - 1e-6);
      }
    }
  });

  it("lays children out left to right without overlapping", () => {
    for (const node of flatten(buildDiffTree(diff, "both"))) {
      let edge = node.start;
      for (const child of node.children) {
        expect(child.start).toBeGreaterThanOrEqual(edge - 1e-6);
        edge = child.start + child.value;
      }
      expect(edge).toBeLessThanOrEqual(node.start + node.value + 1e-6);
    }
  });

  it("orders children the same way the single-run graph does", () => {
    // Stability across runs is the only reason the layout is alphabetical,
    // and it is what makes two graphs comparable by eye.
    const root = buildDiffTree(diff, "both");
    const run = flatten(root).find((node) => node.name === "run()") as FlameNode;
    const names = run.children.map((child) => child.name);
    expect(names).toEqual([...names].sort());
  });
});

describe("diffColour", () => {
  it("is red for growth and blue for shrinkage", () => {
    expect(diffColour({ a: 1, b: 4, delta: 3 })).toMatch(/^hsl\(4 /);
    expect(diffColour({ a: 4, b: 1, delta: -3 })).toMatch(/^hsl\(212 /);
  });

  it("greys out a frame that did not move", () => {
    const flat = diffColour({ a: 100, b: 100, delta: 0 });
    expect(flat).toBe(diffColour({ a: 5, b: 5, delta: 0 }));
    expect(flat).not.toMatch(/^hsl\(4 /);
  });

  it("scales by the frame's own change, not by the profile's biggest", () => {
    // Two frames that both doubled get the same colour whether they are 40%
    // of the run or 0.4% of it -- width already says how big they are.
    expect(diffColour({ a: 1000, b: 2000, delta: 1000 })).toBe(
      diffColour({ a: 1, b: 2, delta: 1 }),
    );
  });

  it("never returns an invalid colour for an empty node", () => {
    expect(diffColour({ a: 0, b: 0, delta: 0 })).toMatch(/^hsl\(/);
  });
});

describe("formatting", () => {
  it("shows shares as percentages and counts as counts", () => {
    expect(formatValue(0.1234, true)).toBe("12.34%");
    expect(formatValue(1234, false)).toBe("1,234");
  });

  it("refuses to round a real path down to nothing", () => {
    expect(formatValue(0.00000031, true)).toBe("<0.01%");
    expect(formatValue(0, true)).toBe("0.00%");
  });

  it("always signs a delta", () => {
    expect(formatDelta(0.05, true)).toBe("+5.00%");
    expect(formatDelta(-0.05, true)).toBe("−5.00%");
    expect(formatDelta(0, true)).toBe("0.00%");
  });

  it("takes the leaf off a path", () => {
    expect(leafOf("a;b;c")).toBe("c");
    expect(leafOf("alone")).toBe("alone");
  });
});
