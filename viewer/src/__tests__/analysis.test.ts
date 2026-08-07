import { describe, expect, it } from "vitest";
import {
  analyseLocks,
  analyseWakeups,
  blameFrame,
  verdict,
  VERDICT,
} from "../analysis";
import { PATHS, type Manifest } from "../bundle/types";
import type { Bundle } from "../bundle/load";

// -- fixtures ---------------------------------------------------------------

const encoder = new TextEncoder();

function manifest(overrides: Partial<Manifest> = {}): Manifest {
  return {
    schema_version: 1,
    run_id: "20260807T000000Z-test",
    label: "test",
    started_at: "2026-08-07T00:00:00Z",
    duration_s: 60,
    profile: "deep",
    status: "ok",
    target: {
      pid: 1,
      comm: "app",
      thread_count_start: 100,
      thread_count_end: 100,
    },
    probes: [],
    tool_versions: {},
    quality: {
      frame_pointers_ok: true,
      unknown_frame_ratio: 0.01,
      estimated_overhead_pct: 3,
    },
    ...overrides,
  };
}

/** A bundle assembled from documents rather than from an archive. */
function bundle(
  files: Record<string, unknown>,
  overrides: Partial<Manifest> = {},
  threads: Bundle["threads"] = null,
): Bundle {
  const encoded = new Map<string, Uint8Array>();
  for (const [path, value] of Object.entries(files)) {
    encoded.set(
      path,
      encoder.encode(typeof value === "string" ? value : JSON.stringify(value)),
    );
  }
  return {
    key: "k",
    fileName: "test.tgz",
    manifest: manifest(overrides),
    system: null,
    threads,
    files: encoded,
    loadedAt: 0,
  };
}

function table(name: string, columns: string[], rows: unknown[][]) {
  return {
    schema_version: 1,
    kind: "table",
    name,
    columns: columns.map((id) => ({ id, type: "string" })),
    rows,
    total_rows: rows.length,
  };
}

const HOT = "0x7f3e03018240";
const COLD = "0x7f3e03018548";

/**
 * One dominant mutex, taken mostly from TimerWheel::arm().
 *
 * The totals are scaled to the fixture's 100 threads over 60 s -- 6,000
 * thread-seconds -- because futex wait is summed across threads and a figure
 * that ignores that denominator is not a bottleneck, it is a number. 2,100 s
 * of waiting here is 35% of every thread's time, which is what real
 * single-mutex contention actually looks like.
 */
const contendedLocks = {
  [PATHS.futexByAddr]: table(
    "futex_by_addr",
    ["addr", "total_us", "calls", "avg_us"],
    [
      [HOT, 2_100_000_000, 525_000, 4000],
      [COLD, 40_000_000, 41_920, 954],
    ],
  ),
  [PATHS.futexSites]: table(
    "futex_sites",
    ["addr", "stack", "total_us", "calls", "avg_us"],
    [
      [
        HOT,
        "WorkerThread::run();TimerWheel::arm();pthread_mutex_lock;__lll_lock_wait",
        1_900_000_000,
        475_000,
        4000,
      ],
      [
        HOT,
        "WorkerThread::run();TimerWheel::cancel();pthread_mutex_lock;__lll_lock_wait",
        200_000_000,
        50_000,
        4000,
      ],
      [
        COLD,
        "WorkerThread::run();EventQueue::pop();pthread_cond_wait",
        40_000_000,
        41_920,
        954,
      ],
    ],
  ),
};

// -- locks ------------------------------------------------------------------

describe("analyseLocks", () => {
  it("ranks addresses by wait time and attributes each to its call paths", () => {
    const analysis = analyseLocks(bundle(contendedLocks));
    expect(analysis).not.toBeNull();
    expect(analysis!.locks[0]!.addr).toBe(HOT);
    expect(analysis!.locks[0]!.share).toBeCloseTo(2_100 / 2_140, 4);
    expect(analysis!.attributed).toBe(true);
    // Sites are sorted worst first, so the first one is the answer.
    expect(analysis!.locks[0]!.sites[0]!.stack).toContain("TimerWheel::arm()");
    expect(analysis!.locks[0]!.sites).toHaveLength(2);
  });

  it("still ranks addresses when the sites table is missing", () => {
    // An older bundle, or a light profile. The addresses are real; inventing
    // call paths for them would not be.
    const analysis = analyseLocks(
      bundle({ [PATHS.futexByAddr]: contendedLocks[PATHS.futexByAddr] }),
    );
    expect(analysis!.locks).toHaveLength(2);
    expect(analysis!.attributed).toBe(false);
    expect(analysis!.locks[0]!.sites).toEqual([]);
  });

  it("returns null when the run has no futex data at all", () => {
    expect(analyseLocks(bundle({}))).toBeNull();
  });
});

describe("blameFrame", () => {
  it("names the caller, not the mechanism every lock ends up in", () => {
    expect(
      blameFrame("WorkerThread::run();TimerWheel::arm();pthread_mutex_lock;__lll_lock_wait"),
    ).toBe("TimerWheel::arm()");
  });

  it("skips the whole glibc tail, not just the last frame", () => {
    expect(
      blameFrame("run();Queue::pop();pthread_cond_wait;futex_wait;[unknown]"),
    ).toBe("Queue::pop()");
  });

  it("falls back to the leaf when every frame is glibc", () => {
    expect(blameFrame("pthread_mutex_lock;__lll_lock_wait")).toBe("__lll_lock_wait");
  });
});

// -- wakeups ----------------------------------------------------------------

const wakeupDoc = {
  [PATHS.wakeupEdges]: {
    schema_version: 1,
    edges: [
      { from_tid: 101, to_tid: 102, count: 5200 },
      { from_tid: 101, to_tid: 103, count: 4870 },
      { from_tid: 101, to_tid: 104, count: 4610 },
      { from_tid: 102, to_tid: 101, count: 210 },
    ],
  },
};

const threadDoc = {
  schema_version: 1,
  threads: {
    "101": { name: "TimerWheel" },
    "102": { name: "worker1" },
    "103": { name: "worker2" },
    "104": { name: "worker3" },
  },
};

describe("analyseWakeups", () => {
  it("finds the thread that wakes everything", () => {
    const analysis = analyseWakeups(bundle(wakeupDoc, {}, threadDoc as never));
    expect(analysis!.hub!.name).toBe("TimerWheel");
    expect(analysis!.hub!.fanOut).toBe(3);
    expect(analysis!.hubShare).toBeCloseTo(14_680 / 14_890, 4);
  });

  it("marks a waker outside the thread inventory as external", () => {
    const analysis = analyseWakeups(bundle(wakeupDoc));
    // Without meta/threads.json nothing can be named, and calling that
    // "unknown" would suggest a lookup failed rather than that the data is
    // simply not in this bundle.
    expect(analysis!.nodes.every((node) => node.external)).toBe(true);
    expect(analysis!.nodes[0]!.name).toBe("tid 101");
  });

  it("declines to call a one-to-one relationship a hub", () => {
    const single = analyseWakeups(
      bundle({
        [PATHS.wakeupEdges]: {
          schema_version: 1,
          edges: [{ from_tid: 1, to_tid: 2, count: 100 }],
        },
      }),
    );
    expect(single!.hub).toBeNull();
  });
});

// -- the verdict ------------------------------------------------------------

describe("verdict", () => {
  it("names the contended mutex by the code that takes it", () => {
    // The acceptance criterion for the whole milestone: a known hot mutex
    // must be the thing the tool says first, and it must say it as a line of
    // code rather than as an address.
    const result = verdict(bundle(contendedLocks));
    expect(result.kind).toBe("lock");
    expect(result.headline).toContain("TimerWheel::arm()");
    expect(result.evidence.join(" ")).toContain(HOT);
    expect(result.evidence.join(" ")).toMatch(/98% of all futex wait/);
  });

  it("refuses to blame a lock when the waiting is spread", () => {
    const spread = analyseSpread();
    const result = verdict(spread);
    expect(result.kind).not.toBe("lock");
  });

  it("refuses to blame a lock that is hot but cheap", () => {
    // One address holds all of the futex time, but the whole of that time is
    // a rounding error against 100 threads for 60 s. A share test alone
    // would call this a bottleneck.
    const result = verdict(
      bundle({
        [PATHS.futexByAddr]: table(
          "futex_by_addr",
          ["addr", "total_us", "calls", "avg_us"],
          [[HOT, 4_000, 40, 100]],
        ),
      }),
    );
    expect(result.kind).toBe("none");
  });

  it("produces no verdict at all when the stacks are unusable", () => {
    // The most dangerous case: the ranking still looks confident.
    const result = verdict(
      bundle(contendedLocks, {
        quality: {
          frame_pointers_ok: false,
          unknown_frame_ratio: 0.62,
          estimated_overhead_pct: 3,
        },
      }),
    );
    expect(result.kind).toBe("untrustworthy");
    expect(result.headline).toContain("No verdict");
    expect(result.next).toContain("-fno-omit-frame-pointer");
  });

  it("says so, with numbers, when nothing dominates", () => {
    const result = verdict(analyseSpread());
    expect(result.kind).toBe("none");
    expect(result.headline).toContain("No single bottleneck");
    // "Nothing dominates" is a measurement, so it carries its numbers too.
    expect(result.evidence.length).toBeGreaterThan(0);
    expect(result.evidence.join(" ")).toMatch(/hottest lock holds/);
  });

  it("blames the scheduler when threads wait for a CPU rather than a lock", () => {
    const threads: Record<string, unknown> = {};
    for (let tid = 1; tid <= 100; tid += 1) {
      threads[String(tid)] = {
        name: `worker${tid}`,
        start_schedstat: { run_ns: 0, wait_ns: 0, timeslices: 0 },
        // 1.8 s of runqueue wait per thread over 60 s = 30 ms/s.
        end_schedstat: { run_ns: 1e9, wait_ns: 1.8e9, timeslices: 0 },
      };
    }
    const result = verdict(
      bundle({}, {}, { schema_version: 1, threads } as never),
    );
    expect(result.kind).toBe("runqueue");
    expect(result.confidence).toBe("strong");
    expect(result.evidence.join(" ")).toMatch(/30\.0 ms of runqueue wait/);
  });

  it("prefers the specific finding over the vague one", () => {
    // A run with both a dominant lock and mild scheduler pressure. A lock is
    // a line of code; "the scheduler is busy" is barely a finding.
    const threads: Record<string, unknown> = {};
    for (let tid = 1; tid <= 100; tid += 1) {
      threads[String(tid)] = {
        name: `worker${tid}`,
        start_schedstat: { run_ns: 0, wait_ns: 0, timeslices: 0 },
        end_schedstat: { run_ns: 1e9, wait_ns: 0.6e9, timeslices: 0 },
      };
    }
    const result = verdict(
      bundle(contendedLocks, {}, { schema_version: 1, threads } as never),
    );
    expect(result.kind).toBe("lock");
  });

  it("always carries evidence, whatever it concludes", () => {
    for (const fixture of [bundle(contendedLocks), analyseSpread(), bundle({})]) {
      const result = verdict(fixture);
      expect(result.headline.length).toBeGreaterThan(20);
      expect(result.evidence).toBeDefined();
    }
  });

  it("uses thresholds high enough that a false positive is unlikely", () => {
    // Documented rather than tuned in silence: the cost of being wrong here
    // is somebody spending a week on the wrong lock.
    expect(VERDICT.lockShare).toBeGreaterThanOrEqual(0.5);
    expect(VERDICT.hubShare).toBeGreaterThanOrEqual(0.5);
  });
});

/** A run where the futex time is spread across many addresses. */
function analyseSpread(): Bundle {
  const rows = Array.from({ length: 20 }, (_, i) => [
    `0x7f3e030${(0x18240 + i * 8).toString(16)}`,
    400_000 - i * 1_000,
    5_000,
    80,
  ]);
  return bundle({
    [PATHS.futexByAddr]: table(
      "futex_by_addr",
      ["addr", "total_us", "calls", "avg_us"],
      rows,
    ),
  });
}
