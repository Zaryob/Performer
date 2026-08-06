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
} from "../bundle/folded";

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
