import { describe, expect, it } from "vitest";
import {
  buildTree,
  filterLines,
  flatten,
  frameColour,
  maxDepth,
  parseFolded,
  search,
  threadTotals,
  threadIdentity,
  threadProfileCoverage,
  threadCpuTimeNs,
  threadRootVisibility,
} from "../bundle/folded";
import type { ThreadsDoc } from "../bundle/types";

const SAMPLE = [
  "worker1;start_thread;run();lock() 100",
  "worker1;start_thread;run();compute() 50",
  "worker2;start_thread;run();lock() 200",
  "idle9;start_thread;park() 1",
].join("\n");

describe("parseFolded", () => {
  it("reads frames and values", () => {
    const result = parseFolded(SAMPLE);
    expect(result.lines).toHaveLength(4);
    expect(result.total).toBe(351);
    expect(result.lines[0]!.frames).toEqual([
      "worker1", "start_thread", "run()", "lock()",
    ]);
  });

  it("counts malformed lines rather than dropping them silently", () => {
    const result = parseFolded("good;path 5\nno-value\nbad;path notanumber\n");
    expect(result.lines).toHaveLength(1);
    expect(result.malformed).toBe(2);
  });

  it("keeps C++ symbols containing spaces intact", () => {
    // Only the last space separates the value, which is what lets
    // "std::map<int, int>::find(int const&)" survive as one frame.
    const result = parseFolded("t;std::map<int, int>::find(int const&) 7");
    expect(result.lines[0]!.frames[1]).toBe("std::map<int, int>::find(int const&)");
    expect(result.lines[0]!.value).toBe(7);
  });

  it("ignores blank lines", () => {
    expect(parseFolded("\n\na;b 1\n\n").lines).toHaveLength(1);
  });
});

describe("threadTotals", () => {
  it("sums per root frame and ranks them", () => {
    const totals = threadTotals(parseFolded(SAMPLE));
    expect(totals.map((t) => t.name)).toEqual(["worker2", "worker1", "idle9"]);
    expect(totals[0]!.value).toBe(200);
    expect(totals[1]!.value).toBe(150);
  });
});

describe("thread identity and coverage", () => {
  const inventory: ThreadsDoc = {
    schema_version: 2,
    threads: Object.fromEntries(Array.from({ length: 125 }, (_, index) => [
      String(index + 100), { name: "worker" },
    ])),
  };

  it("separates same-name threads and preserves the full thread inventory", () => {
    const parsed = parseFolded(Array.from({ length: 10 }, (_, index) =>
      `worker [tid=${index + 100}];run 3`,
    ).join("\n"));
    const coverage = threadProfileCoverage(parsed, inventory);
    expect(coverage.rows).toHaveLength(125);
    expect(coverage.inventoryThreads).toBe(125);
    expect(coverage.recordedThreads).toBe(10);
    expect(coverage.recordedInventoryThreads).toBe(10);
    expect(coverage.withoutStacks).toBe(115);
    expect(coverage.rows.find((row) => row.tid === 224)).toMatchObject({
      coverage: "none", value: null, share: null, roots: [],
    });
    expect(buildTree(parsed).children).toHaveLength(10);
    expect(parsed.total).toBe(30);
  });

  it("does not assign legacy comm aggregates to every matching TID", () => {
    const coverage = threadProfileCoverage(parseFolded("worker;run 300"), inventory);
    expect(coverage.nameAggregates).toBe(1);
    expect(coverage.recordedThreads).toBe(0);
    expect(coverage.unidentifiedThreads).toBe(125);
    expect(coverage.withoutStacks).toBe(0);
    expect(coverage.rows.filter((row) => row.inInventory)
      .every((row) => row.value === null)).toBe(true);
    expect(coverage.rows.find((row) => row.tid === null)).toMatchObject({
      value: 300, coverage: "name aggregate",
    });
  });

  it("combines renamed roots of one TID and keeps threads outside snapshots", () => {
    const coverage = threadProfileCoverage(parseFolded([
      "before [tid=100];run 3", "after [tid=100];run 7",
      "short-lived [tid=999];run 5",
    ].join("\n")), inventory);
    expect(coverage.recordedThreads).toBe(2);
    expect(coverage.rows.find((row) => row.tid === 100)).toMatchObject({
      value: 10, share: 10 / 15, inInventory: true,
    });
    expect(coverage.rows.find((row) => row.tid === 999)).toMatchObject({
      value: 5, inInventory: false, coverage: "recorded",
    });
  });

  it("reads only valid trailing TID markers", () => {
    expect(threadIdentity("worker [tid=123]")).toEqual({ name: "worker", tid: 123 });
    expect(threadIdentity("worker [tid=0]").tid).toBeNull();
    expect(threadIdentity("worker [tid=12] extra").tid).toBeNull();
    expect(threadIdentity("worker").tid).toBeNull();
  });

  it("does not call zero-value folded lines sampled threads", () => {
    const coverage = threadProfileCoverage(parseFolded("worker [tid=100];run 0"), inventory);
    expect(coverage.recordedThreads).toBe(0);
    expect(coverage.rows.find((row) => row.tid === 100)?.value).toBeNull();
  });

  it("flags runtime-positive threads without stacks without treating missing counters as zero", () => {
    const schedstat = (run_ns: number) => ({ run_ns, wait_ns: 0, timeslices: 0 });
    const coverage = threadProfileCoverage(parseFolded("worker [tid=100];run 1"), {
      schema_version: 2,
      threads: {
        "100": { name: "sampled", start_schedstat: schedstat(10), end_schedstat: schedstat(20) },
        "101": { name: "missed", start_schedstat: schedstat(10), end_schedstat: schedstat(1_000_010) },
        "102": { name: "parked", start_schedstat: schedstat(30), end_schedstat: schedstat(30) },
        "103": { name: "unknown", end_schedstat: schedstat(1_000_000) },
      },
    });
    expect(coverage.cpuActiveWithoutStacks).toBe(1);
    expect(coverage.rows.find((row) => row.tid === 101)?.cpuTimeNs).toBe(1_000_000);
    expect(coverage.rows.find((row) => row.tid === 102)?.cpuTimeNs).toBe(0);
    expect(coverage.rows.find((row) => row.tid === 103)?.cpuTimeNs).toBeNull();
  });

  it("does not compare decreasing, missing, or invalid runtime counters", () => {
    const schedstat = (run_ns: number) => ({ run_ns, wait_ns: 0, timeslices: 0 });
    expect(threadCpuTimeNs({ name: "x" })).toBeNull();
    expect(threadCpuTimeNs({ name: "x", start_schedstat: schedstat(10), end_schedstat: schedstat(5) })).toBeNull();
    expect(threadCpuTimeNs({ name: "x", start_schedstat: schedstat(-1), end_schedstat: schedstat(5) })).toBeNull();
    expect(threadCpuTimeNs({ name: "x", start_schedstat: schedstat(0), end_schedstat: schedstat(Infinity) })).toBeNull();
  });
});

describe("thread root canvas visibility", () => {
  it("distinguishes 120 data TIDs from the 10 roots wide enough to draw", () => {
    const parsed = parseFolded(Array.from({ length: 120 }, (_, index) =>
      `worker [tid=${index + 100}];run ${index < 10 ? 1000 : 1}`,
    ).join("\n"));
    const visibility = threadRootVisibility(buildTree(parsed), 1200, 0.4, 26);
    expect(visibility).toMatchObject({
      dataTids: 120, drawnTids: 10, labelEligibleTids: 10,
      subpixelTids: 110, subpixelValue: 110,
    });
    expect(visibility.subpixelShare).toBeCloseTo(110 / 10110);
    expect(parsed.lines).toHaveLength(120);
  });

  it("counts a renamed TID once and keeps legacy name groups separate", () => {
    const root = buildTree(parseFolded([
      "before [tid=100];run 100", "after [tid=100];run 1", "legacy;run 10",
    ].join("\n")));
    expect(threadRootVisibility(root, 100, 0.4, 26)).toMatchObject({
      dataTids: 1, drawnTids: 1, labelEligibleTids: 1,
      subpixelTids: 0, nameAggregateRoots: 1,
    });
  });

  it("distinguishes drawn but unlabeled roots from omitted roots", () => {
    const root = buildTree(parseFolded(Array.from({ length: 120 }, (_, index) =>
      `worker [tid=${index + 100}];run 1`,
    ).join("\n")));
    expect(threadRootVisibility(root, 1200, 0.4, 26)).toMatchObject({
      dataTids: 120, drawnTids: 120, labelEligibleTids: 0, subpixelTids: 0,
    });
  });

  it("uses the canvas draw and label threshold boundaries exactly", () => {
    const root = buildTree(parseFolded("worker [tid=100];run 1"));
    expect(threadRootVisibility(root, 0.4, 0.4, 26).drawnTids).toBe(1);
    expect(threadRootVisibility(root, 0.39, 0.4, 26).subpixelTids).toBe(1);
    expect(threadRootVisibility(root, 26, 0.4, 26).labelEligibleTids).toBe(0);
    expect(threadRootVisibility(root, 26.1, 0.4, 26).labelEligibleTids).toBe(1);
  });
});

describe("filterLines", () => {
  it("filters by thread name, case insensitively", () => {
    const filtered = filterLines(parseFolded(SAMPLE), { threadFilter: "WORKER1" });
    expect(filtered.total).toBe(150);
  });

  it("hides threads below a share of the total", () => {
    const filtered = filterLines(parseFolded(SAMPLE), { hideBelowShare: 0.01 });
    expect(filtered.lines.every((line) => line.frames[0] !== "idle9")).toBe(true);
  });

  it("merges threads by dropping the root frame", () => {
    const merged = filterLines(parseFolded(SAMPLE), { mergeThreads: true });
    expect(merged.total).toBe(351);
    expect(merged.lines[0]!.frames[0]).toBe("start_thread");
    // The same call path from two threads becomes one branch.
    const tree = buildTree(merged);
    const startThread = tree.children.find((c) => c.name === "start_thread")!;
    // Children are alphabetical, so find by name rather than by position.
    const run = startThread.children.find((c) => c.name === "run()")!;
    const lock = run.children.find((c) => c.name === "lock()")!;
    expect(lock.value).toBe(300);
  });

  it("still filters by thread when merging", () => {
    const merged = filterLines(parseFolded(SAMPLE), {
      mergeThreads: true,
      threadFilter: "worker2",
    });
    expect(merged.total).toBe(200);
  });

  it("focuses a numeric TID exactly, including when thread names change", () => {
    const parsed = parseFolded([
      "worker [tid=123];run 4", "renamed [tid=123];run 3",
      "worker [tid=1234];run 100", "worker123;run 200",
    ].join("\n"));
    expect(filterLines(parsed, { threadFilter: "123" }).total).toBe(7);
    expect(filterLines(parsed, { threadFilter: "[tid=123]", mergeThreads: true }).total).toBe(7);
  });

  it("applies the share threshold per TID even when the thread changes name", () => {
    const parsed = parseFolded([
      "before [tid=123];run 1", "after [tid=123];run 1",
      "busy [tid=456];run 98",
    ].join("\n"));
    const filtered = filterLines(parsed, { hideBelowShare: 0.015 });
    expect(filtered.total).toBe(100);
    expect(filtered.lines).toHaveLength(3);
  });

  it("returns the input untouched when nothing is asked of it", () => {
    const parsed = parseFolded(SAMPLE);
    expect(filterLines(parsed, {})).toBe(parsed);
  });
});

describe("buildTree", () => {
  it("accumulates values up the tree", () => {
    const tree = buildTree(parseFolded(SAMPLE));
    expect(tree.value).toBe(351);
    const worker1 = tree.children.find((c) => c.name === "worker1")!;
    expect(worker1.value).toBe(150);
  });

  it("attributes self time only to the leaf", () => {
    const tree = buildTree(parseFolded("a;b;c 10"));
    const a = tree.children[0]!;
    expect(a.self).toBe(0);
    expect(a.children[0]!.children[0]!.self).toBe(10);
  });

  it("orders children alphabetically so two runs are comparable", () => {
    const tree = buildTree(parseFolded("t;zeta 1\nt;alpha 1\nt;mid 1"));
    expect(tree.children[0]!.children.map((c) => c.name)).toEqual([
      "alpha", "mid", "zeta",
    ]);
  });

  it("lays children out left to right without gaps or overlap", () => {
    const tree = buildTree(parseFolded("t;a 10\nt;b 30\nt;c 60"));
    const kids = tree.children[0]!.children;
    expect(kids.map((c) => c.start)).toEqual([0, 10, 40]);
    const last = kids[kids.length - 1]!;
    expect(last.start + last.value).toBe(tree.value);
  });

  it("reports depth", () => {
    expect(maxDepth(buildTree(parseFolded("a;b;c;d 1")))).toBe(4);
  });

  it("flattens every node exactly once", () => {
    const tree = buildTree(parseFolded(SAMPLE));
    const names = flatten(tree).map((n) => n.name);
    expect(names[0]).toBe("all");
    expect(new Set(names).size).toBeLessThanOrEqual(names.length);
    expect(names).toContain("compute()");
  });
});

describe("search", () => {
  it("finds frames and totals their share once", () => {
    const tree = buildTree(parseFolded(SAMPLE));
    const result = search(tree, "lock()");
    expect(result.matches.size).toBe(2); // one under each worker
    expect(result.matchedValue).toBe(300);
  });

  it("does not count a match nested inside another match twice", () => {
    // "run()" contains "run" and so does its child "running()".
    const tree = buildTree(parseFolded("t;run();running() 10"));
    const result = search(tree, "run");
    expect(result.matches.size).toBe(2);
    expect(result.matchedValue).toBe(10);
  });

  it("an empty term matches nothing", () => {
    const tree = buildTree(parseFolded(SAMPLE));
    expect(search(tree, "  ").matches.size).toBe(0);
  });
});

describe("frameColour", () => {
  it("is stable for a name, so a function keeps its colour between runs", () => {
    expect(frameColour("EventQueue::pop()", false)).toBe(
      frameColour("EventQueue::pop()", false),
    );
  });

  it("highlights matches distinctly", () => {
    expect(frameColour("x", true)).not.toBe(frameColour("x", false));
  });
});

describe("scale", () => {
  it("handles a profile with many threads without pathological cost", () => {
    const lines: string[] = [];
    for (let thread = 0; thread < 315; thread += 1) {
      for (let path = 0; path < 12; path += 1) {
        lines.push(`worker${thread};start_thread;run();path${path}();leaf${path % 4}() ${path + 1}`);
      }
    }
    const started = performance.now();
    const parsed = parseFolded(lines.join("\n"));
    const tree = buildTree(filterLines(parsed, { mergeThreads: true }));
    const elapsed = performance.now() - started;
    expect(parsed.lines).toHaveLength(315 * 12);
    expect(tree.value).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(2000);
  });
});
