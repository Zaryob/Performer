/**
 * Reaching a conclusion.
 *
 * Every screen so far presents evidence and leaves the reader to weigh it.
 * That is the right default and it is not enough: the person who most needs
 * this tool is the one who does not already know that a 90% share of futex
 * time at one address means a single contended mutex. So this module says so,
 * in a sentence, with the numbers it rests on attached.
 *
 * Three rules keep that from becoming a liability:
 *
 *   1. **A verdict is a claim, so it carries its evidence.** Never "your
 *      problem is lock contention" alone; always the share, the address, the
 *      call path, and where to look next.
 *   2. **No verdict is a valid answer.** A profile where nothing dominates is
 *      the common case and the honest output is "nothing dominates" -- not
 *      the largest of several small things dressed up as a finding.
 *   3. **Unusable data produces no verdict at all.** A run whose stacks are
 *      unresolved can still produce a confident-looking ranking, and that is
 *      exactly when a machine-written sentence does the most damage.
 */

import type { Bundle } from "./bundle/load";
import { readJson, readText, runDuration } from "./bundle/load";
import { PATHS, type Manifest, type TableDoc } from "./bundle/types";
import { parseFolded, threadTotals } from "./bundle/folded";
import { stacksAreTrustworthy } from "./quality";

// -- locks ------------------------------------------------------------------

export interface LockSite {
  /** Call path that waited, root first, `;` separated. */
  stack: string;
  totalUs: number;
  calls: number;
  avgUs: number | null;
}

export interface Lock {
  /** The futex word's address. An identity within one run, nothing more. */
  addr: string;
  totalUs: number;
  calls: number;
  avgUs: number | null;
  /** Share of all futex wait time in the run. */
  share: number;
  /** Call paths that waited on this address, worst first. */
  sites: LockSite[];
}

export interface LockAnalysis {
  locks: Lock[];
  totalUs: number;
  /** True when the sites table was present, so attribution is real. */
  attributed: boolean;
  truncated: boolean;
}

function column(doc: TableDoc, id: string): number {
  return doc.columns.findIndex((entry) => entry.id === id);
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/**
 * Rank contended locks, and attribute each to the code that takes it.
 *
 * The two source tables are separate aggregations in the probe and cannot be
 * joined afterwards, which is why the probe also emits the combined one. When
 * only `futex_by_addr` is present -- an older bundle -- the addresses are
 * still ranked, and `attributed` says the call paths are missing rather than
 * the screen inventing them.
 */
export function analyseLocks(bundle: Bundle): LockAnalysis | null {
  const byAddr = readJson<TableDoc>(bundle, PATHS.futexByAddr);
  const sites = readJson<TableDoc>(bundle, PATHS.futexSites);
  if (!byAddr && !sites) return null;

  const grouped = new Map<string, Lock>();
  let totalUs = 0;

  if (byAddr) {
    const iAddr = column(byAddr, "addr");
    const iTotal = column(byAddr, "total_us");
    const iCalls = column(byAddr, "calls") >= 0 ? column(byAddr, "calls") : column(byAddr, "count");
    const iAvg = column(byAddr, "avg_us");
    for (const row of byAddr.rows) {
      const addr = String(row[iAddr] ?? "?");
      const total = num(row[iTotal]);
      grouped.set(addr, {
        addr,
        totalUs: total,
        calls: num(row[iCalls]),
        avgUs: iAvg >= 0 ? num(row[iAvg]) : null,
        share: 0,
        sites: [],
      });
      totalUs += total;
    }
  }

  if (sites) {
    const iAddr = column(sites, "addr");
    const iStack = column(sites, "stack");
    const iTotal = column(sites, "total_us");
    const iCalls = column(sites, "calls");
    const iAvg = column(sites, "avg_us");
    for (const row of sites.rows) {
      const addr = String(row[iAddr] ?? "?");
      const total = num(row[iTotal]);
      let lock = grouped.get(addr);
      if (!lock) {
        // A bundle with sites but no address table: still usable, and the
        // totals come out of the sites themselves.
        lock = { addr, totalUs: 0, calls: 0, avgUs: null, share: 0, sites: [] };
        grouped.set(addr, lock);
        totalUs += total;
        lock.totalUs = total;
      }
      lock.sites.push({
        stack: String(row[iStack] ?? ""),
        totalUs: total,
        calls: num(row[iCalls]),
        avgUs: iAvg >= 0 ? num(row[iAvg]) : null,
      });
    }
  }

  const locks = [...grouped.values()];
  for (const lock of locks) {
    lock.share = totalUs > 0 ? lock.totalUs / totalUs : 0;
    lock.sites.sort((a, b) => b.totalUs - a.totalUs);
  }
  locks.sort((a, b) => b.totalUs - a.totalUs);

  return {
    locks,
    totalUs,
    attributed: Boolean(sites),
    truncated: Boolean(byAddr?.truncated || sites?.truncated),
  };
}

/** The frame a developer would recognise: the deepest one that is not glibc. */
export function blameFrame(stack: string): string {
  const frames = stack.split(";").filter(Boolean);
  for (let i = frames.length - 1; i >= 0; i -= 1) {
    const frame = frames[i] as string;
    if (!GLIBC_RE.test(frame)) return frame;
  }
  return frames[frames.length - 1] ?? "?";
}

/**
 * Frames that are the *mechanism* of waiting rather than the reason for it.
 *
 * `__lll_lock_wait` is where every contended mutex ends up, so naming it as
 * the culprit is true and useless. The caller above it is the answer.
 */
const GLIBC_RE =
  /^(__lll_lock_wait|pthread_mutex_lock|pthread_mutex_timedlock|pthread_cond_wait|pthread_cond_timedwait|futex_wait|__futex_abstimed_wait\w*|syscall|__syscall\w*|\[unknown\])$/;

// -- wakeups ----------------------------------------------------------------

export interface WakeNode {
  tid: number;
  name: string;
  out: number;
  in: number;
  /** Distinct threads this one wakes. */
  fanOut: number;
  external: boolean;
}

export interface WakeEdge {
  from: number;
  to: number;
  count: number;
}

export interface WakeAnalysis {
  nodes: WakeNode[];
  edges: WakeEdge[];
  totalWakes: number;
  truncated: boolean;
  /** The thread that wakes the most others, when one clearly does. */
  hub: WakeNode | null;
  /** Share of all wakeups the hub is responsible for. */
  hubShare: number;
}

export function analyseWakeups(bundle: Bundle): WakeAnalysis | null {
  const doc = readJson<{
    edges: { from_tid: number; to_tid: number; count: number }[];
    nodes?: { tid: number; name?: string; external?: boolean }[];
    truncated?: boolean;
  }>(bundle, PATHS.wakeupEdges);
  if (!doc || !Array.isArray(doc.edges)) return null;

  const names = new Map<number, string>();
  for (const [tid, entry] of Object.entries(bundle.threads?.threads ?? {})) {
    names.set(Number(tid), entry.name);
  }
  for (const node of doc.nodes ?? []) {
    if (node.name) names.set(node.tid, node.name);
  }

  const nodes = new Map<number, WakeNode>();
  const ensure = (tid: number): WakeNode => {
    let node = nodes.get(tid);
    if (!node) {
      node = {
        tid,
        // A waker outside the thread inventory is a different process (or the
        // kernel), and calling it "unknown" would suggest a lookup failure.
        name: names.get(tid) ?? `tid ${tid}`,
        out: 0,
        in: 0,
        fanOut: 0,
        external: !names.has(tid),
      };
      nodes.set(tid, node);
    }
    return node;
  };

  const edges: WakeEdge[] = [];
  let totalWakes = 0;
  for (const edge of doc.edges) {
    const from = ensure(edge.from_tid);
    const to = ensure(edge.to_tid);
    const count = num(edge.count);
    from.out += count;
    from.fanOut += 1;
    to.in += count;
    totalWakes += count;
    edges.push({ from: edge.from_tid, to: edge.to_tid, count });
  }

  const ranked = [...nodes.values()].sort((a, b) => b.out - a.out || b.fanOut - a.fanOut);
  const hub = ranked[0] ?? null;
  return {
    nodes: ranked,
    edges,
    totalWakes,
    truncated: Boolean(doc.truncated),
    hub: hub && hub.fanOut > 1 ? hub : null,
    hubShare: hub && totalWakes > 0 ? hub.out / totalWakes : 0,
  };
}

// -- the verdict ------------------------------------------------------------

export type VerdictKind =
  | "lock"
  | "allocator"
  | "runqueue"
  | "wakeup-hub"
  | "cpu"
  | "none"
  | "untrustworthy";

export interface Verdict {
  kind: VerdictKind;
  /** The claim, in one sentence. */
  headline: string;
  /** The numbers it rests on. A verdict without these is an opinion. */
  evidence: string[];
  /** Where to look next, phrased as an action. */
  next?: string;
  /** How strongly the data supports it. */
  confidence: "strong" | "moderate" | "weak";
}

/**
 * Thresholds for calling something the bottleneck.
 *
 * These are deliberately high. The cost of a false positive here is someone
 * spending a week on the wrong lock; the cost of a false negative is that
 * they read the screens themselves, which they were going to do anyway.
 */
export const VERDICT = {
  /** One address holding this share of futex time is a single contended lock. */
  lockShare: 0.5,
  /** Below this, the futex time itself is not worth a verdict. */
  lockOfWallclock: 0.15,
  /** Share of on-CPU samples under an allocator path. */
  allocatorShare: 0.15,
  /** Runqueue wait per thread per second, in ms, that means CPU starvation. */
  runqueueMsPerThreadPerS: 5,
  /** Share of wakeups from a single thread that makes it a serialising hub. */
  hubShare: 0.5,
} as const;

const ALLOCATOR_RE =
  /(_int_malloc|_int_free|arena_get|malloc_consolidate|tcache|operator new|operator delete|je_malloc|tc_malloc)/i;

/**
 * Name the dominant bottleneck, or decline to.
 *
 * The order is not arbitrary: it runs from the most specific and most
 * actionable finding to the least. A single contended mutex is a line of
 * code; "the process is CPU bound" is barely a finding at all and comes last.
 */
export function verdict(bundle: Bundle): Verdict {
  const manifest = bundle.manifest;

  if (!stacksAreTrustworthy(manifest)) {
    return {
      kind: "untrustworthy",
      headline:
        "No verdict: this run's stacks could not be resolved, so any ranking " +
        "of call paths would be a ranking of truncated ones.",
      evidence: [
        `${(manifest.quality.unknown_frame_ratio * 100).toFixed(0)}% of frames are [unknown]`,
      ],
      next: "Rebuild the target with -fno-omit-frame-pointer and measure again.",
      confidence: "strong",
    };
  }

  const seconds = runDuration(manifest);
  const candidates: Verdict[] = [];

  // -- a single contended lock ---------------------------------------------
  const locks = analyseLocks(bundle);
  if (locks && locks.locks.length > 0 && seconds > 0) {
    const worst = locks.locks[0] as Lock;
    // Futex wait is summed across threads, so it can exceed wall clock many
    // times over and comparing it to the run length alone would be
    // meaningless. Per thread is the number that means something.
    const threads = Math.max(1, manifest.target.thread_count_start);
    const waitPerThread = worst.totalUs / 1e6 / threads / seconds;
    if (worst.share >= VERDICT.lockShare && waitPerThread >= VERDICT.lockOfWallclock) {
      const site = worst.sites[0];
      const blame = site ? blameFrame(site.stack) : null;
      const evidence = [
        `${(worst.share * 100).toFixed(0)}% of all futex wait time is on one address (${worst.addr})`,
        `${(worst.totalUs / 1e6).toFixed(1)} s of waiting across ${worst.calls.toLocaleString()} waits` +
          ` — ${(waitPerThread * 100).toFixed(0)}% of each thread's time, on average`,
      ];
      if (site) {
        evidence.push(
          `${((site.totalUs / worst.totalUs) * 100).toFixed(0)}% of that address's wait comes from ${blame}`,
        );
      }
      candidates.push({
        kind: "lock",
        headline: blame
          ? `A single mutex is the bottleneck: the one taken by ${blame}.`
          : `A single mutex is the bottleneck: the one at ${worst.addr}.`,
        evidence,
        next: locks.attributed
          ? "Locks — the call paths that take it are listed under that address."
          : "Locks — this run has no call-path attribution; collect with the deep profile to get it.",
        confidence: worst.share >= 0.75 && locks.attributed ? "strong" : "moderate",
      });
    }
  }

  // -- allocator contention -------------------------------------------------
  const oncpuText = readText(bundle, PATHS.oncpu);
  if (oncpuText) {
    const parsed = parseFolded(oncpuText);
    let allocator = 0;
    for (const line of parsed.lines) {
      if (line.frames.some((frame) => ALLOCATOR_RE.test(frame))) allocator += line.value;
    }
    const share = parsed.total > 0 ? allocator / parsed.total : 0;
    if (share >= VERDICT.allocatorShare) {
      candidates.push({
        kind: "allocator",
        headline:
          "The allocator is a substantial share of on-CPU time, which in a " +
          "thread pool this size usually means malloc arena contention.",
        evidence: [
          `${(share * 100).toFixed(0)}% of on-CPU samples are inside allocator frames`,
          `${threadCountText(manifest)} sharing the process's arenas`,
        ],
        next:
          "Flame — search for arena_get2. If it is present, raise MALLOC_ARENA_MAX or " +
          "pool the allocations that dominate.",
        confidence: share >= 0.3 ? "strong" : "moderate",
      });
    }
  }

  // -- scheduler pressure ---------------------------------------------------
  const runq = runqueuePressure(bundle, seconds);
  if (runq !== null && runq.msPerThreadPerS >= VERDICT.runqueueMsPerThreadPerS) {
    candidates.push({
      kind: "runqueue",
      headline:
        "Threads are waiting for a CPU rather than for each other: this is " +
        "scheduler pressure from having more runnable threads than cores.",
      evidence: [
        `${runq.msPerThreadPerS.toFixed(1)} ms of runqueue wait per thread per second`,
        `${threadCountText(manifest)}${runq.cpus ? ` on ${runq.cpus} CPUs` : ""}`,
      ],
      next: "Threads — sort by runqueue ms to see which pool is oversubscribed.",
      confidence: runq.msPerThreadPerS >= 15 ? "strong" : "moderate",
    });
  }

  // -- one thread waking everything ----------------------------------------
  const wakeups = analyseWakeups(bundle);
  if (wakeups?.hub && wakeups.hubShare >= VERDICT.hubShare) {
    candidates.push({
      kind: "wakeup-hub",
      headline: `One thread (${wakeups.hub.name}) wakes most of the process, so it serialises everything downstream of it.`,
      evidence: [
        `${(wakeups.hubShare * 100).toFixed(0)}% of all wakeups come from ${wakeups.hub.name}`,
        `it wakes ${wakeups.hub.fanOut.toLocaleString()} distinct threads`,
      ],
      next: "Wakeups — the graph shows what it feeds.",
      confidence: wakeups.hubShare >= 0.75 ? "strong" : "moderate",
    });
  }

  if (candidates.length === 0) {
    return {
      kind: "none",
      headline:
        "No single bottleneck dominates this run. That is a finding, not a " +
        "gap: the cost is spread, so there is no one thing to fix.",
      evidence: evidenceForNothing(bundle, locks, seconds),
      next: "Flame — the widest frames are where the time actually goes.",
      confidence: "moderate",
    };
  }
  return candidates[0] as Verdict;
}

function threadCountText(manifest: Manifest): string {
  const start = manifest.target.thread_count_start;
  const end = manifest.target.thread_count_end;
  return start === end
    ? `${start.toLocaleString()} threads`
    : `${start.toLocaleString()} → ${end.toLocaleString()} threads`;
}

function runqueuePressure(
  bundle: Bundle,
  seconds: number,
): { msPerThreadPerS: number; cpus: number | null } | null {
  const threads = bundle.threads?.threads;
  if (!threads || seconds <= 0) return null;
  let waitNs = 0;
  let counted = 0;
  for (const entry of Object.values(threads)) {
    const start = entry.start_schedstat?.wait_ns ?? 0;
    const end = entry.end_schedstat?.wait_ns ?? 0;
    if (end >= start) waitNs += end - start;
    counted += 1;
  }
  if (!counted) return null;
  return {
    msPerThreadPerS: waitNs / 1e6 / counted / seconds,
    cpus: bundle.system?.cpu_count ?? null,
  };
}

/** What "nothing dominates" is based on, so it is a measurement too. */
function evidenceForNothing(
  bundle: Bundle,
  locks: LockAnalysis | null,
  seconds: number,
): string[] {
  const evidence: string[] = [];
  if (locks && locks.locks.length) {
    const worst = locks.locks[0] as Lock;
    evidence.push(
      `hottest lock holds ${(worst.share * 100).toFixed(0)}% of futex wait ` +
        `(a single contended mutex would be over ${VERDICT.lockShare * 100}%)`,
    );
  }
  const runq = runqueuePressure(bundle, seconds);
  if (runq) {
    evidence.push(
      `${runq.msPerThreadPerS.toFixed(1)} ms runqueue wait per thread per second ` +
        `(pressure starts around ${VERDICT.runqueueMsPerThreadPerS})`,
    );
  }
  const oncpu = readText(bundle, PATHS.oncpu);
  if (oncpu) {
    const totals = threadTotals(parseFolded(oncpu));
    if (totals.length) {
      const top = totals[0] as { name: string; share: number };
      evidence.push(
        `busiest thread ${top.name} holds ${(top.share * 100).toFixed(0)}% of on-CPU samples`,
      );
    }
  }
  return evidence;
}
