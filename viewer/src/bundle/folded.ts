/**
 * Folded stacks -> a flame graph tree.
 *
 * The format is one stack per line, root first, `;` separated, value last:
 *
 *     app;start_thread;WorkerThread::run();EventQueue::pop() 842
 *
 * The collector puts the thread name in the first frame, which is what makes
 * the thread filter on the Flame screen possible at all -- and that filter is
 * not a nicety. A pool of 315 threads, most of them parked, produces a graph
 * where the idle wait dwarfs everything anyone wants to see.
 */

export interface FoldedLine {
  frames: string[];
  value: number;
}

export interface FlameNode {
  name: string;
  /** Total value of this subtree. */
  value: number;
  /** Value attributed to this frame itself rather than to anything it called. */
  self: number;
  depth: number;
  /** Left edge, in value units, assigned by the layout pass. */
  start: number;
  children: FlameNode[];
}

export interface ParseResult {
  lines: FoldedLine[];
  total: number;
  /** Lines that could not be read, kept as a count rather than dropped quietly. */
  malformed: number;
}

export function parseFolded(text: string): ParseResult {
  const lines: FoldedLine[] = [];
  let total = 0;
  let malformed = 0;

  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const cut = line.lastIndexOf(" ");
    if (cut <= 0) {
      malformed += 1;
      continue;
    }
    const value = Number(line.slice(cut + 1));
    if (!Number.isFinite(value) || value < 0) {
      malformed += 1;
      continue;
    }
    lines.push({ frames: line.slice(0, cut).split(";"), value });
    total += value;
  }
  return { lines, total, malformed };
}

// -- per thread totals ------------------------------------------------------

export interface ThreadTotal {
  name: string;
  value: number;
  share: number;
}

/**
 * Total value per root frame, which for collector-produced stacks is the
 * thread name.
 */
export function threadTotals(result: ParseResult): ThreadTotal[] {
  const totals = new Map<string, number>();
  for (const line of result.lines) {
    const name = line.frames[0] ?? "";
    totals.set(name, (totals.get(name) ?? 0) + line.value);
  }
  const out = [...totals].map(([name, value]) => ({
    name,
    value,
    share: result.total > 0 ? value / result.total : 0,
  }));
  out.sort((a, b) => b.value - a.value);
  return out;
}

// -- filtering --------------------------------------------------------------

export interface FilterOptions {
  /** Case insensitive substring matched against the root (thread) frame. */
  threadFilter?: string;
  /**
   * Drop threads accounting for less than this fraction of the total. Zero
   * keeps everything.
   */
  hideBelowShare?: number;
  /**
   * Drop the thread name frame so identical call paths from different threads
   * combine.
   *
   * With 315 threads the per thread view is a comb of slivers one pixel wide:
   * every thread gets its own root, so a call path taken by all of them is
   * drawn 315 separate times and none of them is wide enough to read. Merging
   * answers "where does the time go" -- which is the first question -- and the
   * thread filter answers "which thread" once there is a reason to ask.
   */
  mergeThreads?: boolean;
}

export function filterLines(
  result: ParseResult,
  options: FilterOptions,
): ParseResult {
  const needle = (options.threadFilter ?? "").trim().toLowerCase();
  const minShare = options.hideBelowShare ?? 0;
  const merge = options.mergeThreads ?? false;
  if (!needle && minShare <= 0 && !merge) return result;

  const keepThread = new Set<string>();
  if (minShare > 0) {
    for (const thread of threadTotals(result)) {
      if (thread.share >= minShare) keepThread.add(thread.name);
    }
  }

  const lines: FoldedLine[] = [];
  let total = 0;
  for (const line of result.lines) {
    // Matched against the original root, so the thread filter keeps working
    // after the thread frame has been merged away.
    const root = line.frames[0] ?? "";
    if (needle && !root.toLowerCase().includes(needle)) continue;
    if (minShare > 0 && !keepThread.has(root)) continue;
    if (merge && line.frames.length > 1) {
      lines.push({ frames: line.frames.slice(1), value: line.value });
    } else {
      lines.push(line);
    }
    total += line.value;
  }
  return { lines, total, malformed: result.malformed };
}

// -- tree -------------------------------------------------------------------

interface BuildNode {
  name: string;
  value: number;
  self: number;
  children: Map<string, BuildNode>;
}

/**
 * Build the tree and lay it out in one pass over the lines.
 *
 * Children are ordered alphabetically rather than by size. That costs a
 * little visual tidiness and buys stability: the same code path sits in the
 * same place in every run, which is what makes two graphs comparable by eye
 * -- and, from M4, diffable at all.
 */
export function buildTree(result: ParseResult, rootName = "all"): FlameNode {
  const root: BuildNode = {
    name: rootName,
    value: 0,
    self: 0,
    children: new Map(),
  };

  for (const line of result.lines) {
    let node = root;
    node.value += line.value;
    for (const frame of line.frames) {
      let child = node.children.get(frame);
      if (!child) {
        child = { name: frame, value: 0, self: 0, children: new Map() };
        node.children.set(frame, child);
      }
      child.value += line.value;
      node = child;
    }
    node.self += line.value;
  }

  const convert = (build: BuildNode, depth: number, start: number): FlameNode => {
    const node: FlameNode = {
      name: build.name,
      value: build.value,
      self: build.self,
      depth,
      start,
      children: [],
    };
    let offset = start;
    const names = [...build.children.keys()].sort();
    for (const name of names) {
      const child = build.children.get(name) as BuildNode;
      node.children.push(convert(child, depth + 1, offset));
      offset += child.value;
    }
    return node;
  };

  return convert(root, 0, 0);
}

/** Depth-first list of every node, which is the order they are drawn in. */
export function flatten(root: FlameNode): FlameNode[] {
  const out: FlameNode[] = [];
  const stack: FlameNode[] = [root];
  while (stack.length) {
    const node = stack.pop() as FlameNode;
    out.push(node);
    for (let i = node.children.length - 1; i >= 0; i -= 1) {
      stack.push(node.children[i] as FlameNode);
    }
  }
  return out;
}

export function maxDepth(root: FlameNode): number {
  let deepest = 0;
  for (const node of flatten(root)) {
    if (node.depth > deepest) deepest = node.depth;
  }
  return deepest;
}

// -- search -----------------------------------------------------------------

export interface SearchResult {
  matches: Set<FlameNode>;
  /** Value covered by matches, not double counting nested ones. */
  matchedValue: number;
}

/**
 * Find every frame whose name contains the term.
 *
 * The matched total skips a match nested inside another match, so "how much
 * of this profile is under `pthread_mutex_lock`" reads as a percentage of the
 * whole rather than counting the same samples once per level.
 */
export function search(root: FlameNode, term: string): SearchResult {
  const matches = new Set<FlameNode>();
  const needle = term.trim().toLowerCase();
  if (!needle) return { matches, matchedValue: 0 };

  let matchedValue = 0;
  const walk = (node: FlameNode, insideMatch: boolean) => {
    const hit = node.name.toLowerCase().includes(needle);
    if (hit) {
      matches.add(node);
      if (!insideMatch) matchedValue += node.value;
    }
    for (const child of node.children) walk(child, insideMatch || hit);
  };
  walk(root, false);
  return { matches, matchedValue };
}

// -- colour -----------------------------------------------------------------

/**
 * The classic flame palette: a warm hue chosen from the frame name, so the
 * same function keeps its colour between runs and between screens.
 */
export function frameColour(name: string, highlighted: boolean): string {
  if (highlighted) return "#8b5cf6";
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  const normalised = (Math.abs(hash) % 1000) / 1000;
  const hue = 12 + normalised * 40; // red through amber
  const saturation = 68 + ((Math.abs(hash) >> 10) % 18);
  const lightness = 46 + ((Math.abs(hash) >> 20) % 14);
  return `hsl(${hue.toFixed(0)} ${saturation}% ${lightness}%)`;
}
