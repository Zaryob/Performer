/**
 * Comparing two runs.
 *
 * The question this exists to answer is never "what does the profile look
 * like" -- the Flame screen answers that -- but "what changed". Those are
 * different questions and a flame graph per run answers the second one badly:
 * two graphs side by side are two arbitrary shapes, and the eye is very
 * willing to see a difference that is really just a different total.
 *
 * So the comparison is computed, not looked at. Every call path is reduced to
 * a single string key, the two runs are joined on that key, and the result is
 * a signed delta per path plus the two sets that matter most in practice: the
 * paths that appear only in the newer run, and the paths that disappeared.
 * Those are usually the finding. A path that grew by 8% is a number; a path
 * that did not exist before is a change of behaviour.
 *
 * The layout follows `difffolded.pl` in intent and departs from it in one
 * place, documented at `WIDTH_BASES` below.
 */

import {
  buildTree,
  threadIdentity,
  type FlameNode,
  type FoldedLine,
  type ParseResult,
} from "./folded";

// -- normalisation ----------------------------------------------------------

/** A trailing `+123` / `+0x7b` offset. Anchored so `operator+` survives. */
const OFFSET_RE = /\+(?:0x[0-9a-fA-F]+|[0-9]+)$/;
/** A bare address the stack walker could not symbolise. */
const ADDRESS_RE = /^0x[0-9a-fA-F]+$/;
/** A trailing module annotation, e.g. `main+66 (/usr/local/bin/app)`. */
const MODULE_RE = /\s+\((?:\/[^)]*)\)$/;

export const UNKNOWN = "[unknown]";

/**
 * Reduce a frame to something two runs can be joined on.
 *
 * This repeats `clean_frame` in `collector/performer/parse/stacks.py`, and
 * repeating it is deliberate. The collector normalises what it writes, so for
 * bundles it produced this is a no-op -- but the viewer also has to cope with
 * folded files that came from `perf script | stackcollapse-perf.pl`, from an
 * older collector, or from a target whose libraries were rebuilt between the
 * two runs. Without this, `_int_malloc+0x1f4` and `_int_malloc+0x2a0` are two
 * different code paths and the diff reports a rewrite where nothing changed.
 */
export function normaliseFrame(frame: string): string {
  let text = frame.trim();
  if (!text) return UNKNOWN;
  text = text.replace(MODULE_RE, "");
  text = text.replace(OFFSET_RE, "");
  if (ADDRESS_RE.test(text)) return UNKNOWN;
  return text;
}

/** Join a normalised stack back into its folded key. */
export function pathKey(frames: string[]): string {
  return frames.map(normaliseFrame).join(";");
}

// -- the comparison ---------------------------------------------------------

export interface PathDelta {
  /** `;` separated normalised frames, root first. */
  path: string;
  /** Value in run A, in whatever unit the comparison is expressed in. */
  a: number;
  /** Value in run B. */
  b: number;
  /** `b - a`. Positive means the newer run spends more here. */
  delta: number;
  /**
   * `delta / a`, or null when the path is new in B and the ratio would be
   * infinite. A null here is not missing data: it is the statement "this did
   * not exist before".
   */
  ratio: number | null;
}

export interface DiffOptions {
  /**
   * Express both runs as a share of their own total rather than in raw units.
   *
   * On by default when the two runs differ in length, because they usually
   * do and the alternative is nonsense: a 120 s run has twice the samples of
   * a 60 s one and every single path "grew".
   */
  normalise?: boolean;
  /** Drop the thread frame so the same call path from different threads combines. */
  mergeThreads?: boolean;
  /** Case insensitive substring matched against the root (thread) frame. */
  threadFilter?: string;
  /**
   * Ignore paths whose share of both totals is below this. Sampling noise on
   * a path worth 0.001% of the profile is not a finding, and there are tens of
   * thousands of such paths in a 315 thread run.
   */
  minShare?: number;
}

export interface DiffResult {
  /** Every path in either run, sorted by |delta| descending. */
  paths: PathDelta[];
  /** Present in B, absent from A. Sorted by value descending. */
  onlyInB: PathDelta[];
  /** Present in A, absent from B. Sorted by value descending. */
  onlyInA: PathDelta[];
  /** Totals in the comparison unit; 1 for each side when normalised. */
  totalA: number;
  totalB: number;
  /** Raw totals before normalisation, for the header. */
  rawTotalA: number;
  rawTotalB: number;
  normalised: boolean;
  /** Sum of the positive deltas: how much of the profile moved. */
  grew: number;
  /** Sum of the negative deltas, as a positive number. */
  shrank: number;
  /** Paths dropped by `minShare`, kept as a count rather than hidden silently. */
  belowThreshold: number;
}

function fold(
  result: ParseResult,
  options: DiffOptions,
): { totals: Map<string, number>; total: number } {
  const needle = (options.threadFilter ?? "").trim().toLowerCase();
  const merge = options.mergeThreads ?? false;
  const totals = new Map<string, number>();
  let total = 0;

  for (const line of result.lines) {
    const root = line.frames[0] ?? "";
    if (needle && !root.toLowerCase().includes(needle)) continue;
    const frames = merge && line.frames.length > 1
      ? line.frames.slice(1)
      : [threadIdentity(root).name, ...line.frames.slice(1)];
    const key = pathKey(frames);
    totals.set(key, (totals.get(key) ?? 0) + line.value);
    total += line.value;
  }
  return { totals, total };
}

export function diffFolded(
  a: ParseResult,
  b: ParseResult,
  options: DiffOptions = {},
): DiffResult {
  const normalised = options.normalise ?? true;
  const minShare = options.minShare ?? 0;

  const left = fold(a, options);
  const right = fold(b, options);

  // Dividing by the total is what makes the two runs comparable at all. It
  // also means a path can shrink as a share while growing in absolute terms,
  // which is a real distinction and the reason the raw totals stay in the
  // result for the header to show.
  const scaleA = normalised && left.total > 0 ? 1 / left.total : 1;
  const scaleB = normalised && right.total > 0 ? 1 / right.total : 1;

  const keys = new Set([...left.totals.keys(), ...right.totals.keys()]);
  const paths: PathDelta[] = [];
  const onlyInA: PathDelta[] = [];
  const onlyInB: PathDelta[] = [];
  let grew = 0;
  let shrank = 0;
  let belowThreshold = 0;

  for (const path of keys) {
    const rawA = left.totals.get(path) ?? 0;
    const rawB = right.totals.get(path) ?? 0;
    if (minShare > 0) {
      const shareA = left.total > 0 ? rawA / left.total : 0;
      const shareB = right.total > 0 ? rawB / right.total : 0;
      if (shareA < minShare && shareB < minShare) {
        belowThreshold += 1;
        continue;
      }
    }
    const valueA = rawA * scaleA;
    const valueB = rawB * scaleB;
    const entry: PathDelta = {
      path,
      a: valueA,
      b: valueB,
      delta: valueB - valueA,
      ratio: rawA > 0 ? (valueB - valueA) / valueA : null,
    };
    paths.push(entry);
    if (entry.delta > 0) grew += entry.delta;
    else shrank -= entry.delta;
    if (rawA === 0 && rawB > 0) onlyInB.push(entry);
    else if (rawB === 0 && rawA > 0) onlyInA.push(entry);
  }

  paths.sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta) || x.path.localeCompare(y.path));
  onlyInB.sort((x, y) => y.b - x.b || x.path.localeCompare(y.path));
  onlyInA.sort((x, y) => y.a - x.a || x.path.localeCompare(y.path));

  return {
    paths,
    onlyInA,
    onlyInB,
    totalA: left.total * scaleA,
    totalB: right.total * scaleB,
    rawTotalA: left.total,
    rawTotalB: right.total,
    normalised,
    grew,
    shrank,
    belowThreshold,
  };
}

// -- the differential tree --------------------------------------------------

/**
 * How wide to draw a frame when the two runs disagree about its size.
 *
 * `difffolded.pl` draws widths from the newer run, which makes a path that
 * *vanished* between the runs zero pixels wide -- invisible, though it is
 * often the most interesting thing that happened. Its answer is a second,
 * negated graph. Here the default is the sum instead: every path present in
 * either run gets width in proportion to its combined weight, so both
 * directions of change are on one graph and nothing is silently missing.
 *
 * The sum is also the only one of the three that is safe for a flame graph
 * layout in general. A parent's width must be at least the sum of its
 * children's, and `a + b` is additive, so that holds by construction. (`max`
 * would be the intuitive choice and is not additive: it can produce children
 * wider than the parent that contains them.)
 *
 * "after" and "before" are kept because they are what the eye expects from a
 * conventional flame graph, and because reading a single run's shape with the
 * delta colouring on top is a genuinely different way of looking at it.
 */
export const WIDTH_BASES = ["both", "after", "before"] as const;
export type WidthBasis = (typeof WIDTH_BASES)[number];

export interface DiffNode extends FlameNode {
  /** Subtree value in run A, in the comparison unit. */
  a: number;
  /** Subtree value in run B. */
  b: number;
  /** `b - a` for this subtree. */
  delta: number;
  children: DiffNode[];
}

/**
 * Build one tree carrying both runs' numbers.
 *
 * Layout comes from `buildTree` so the geometry, the alphabetical child order
 * and the depth-first draw order are shared with the single-run graph rather
 * than reimplemented -- the stability of that ordering is what lets a person
 * flip between the two screens and find the same frame in the same place.
 */
export function buildDiffTree(
  diff: DiffResult,
  basis: WidthBasis = "both",
  rootName = "all",
): DiffNode {
  const width = (entry: PathDelta): number => {
    if (basis === "after") return entry.b;
    if (basis === "before") return entry.a;
    return entry.a + entry.b;
  };

  // Values are fractions once normalised, and a flame graph laid out in
  // fractions loses its width to floating point long before it loses it to
  // the pixel grid. Scaling to a large integer basis keeps the arithmetic in
  // a range where a 0.001% path is still a positive number.
  const scale = diff.normalised ? 1e9 : 1;

  const lines: FoldedLine[] = [];
  const byPath = new Map<string, PathDelta>();
  for (const entry of diff.paths) {
    const value = width(entry) * scale;
    if (value <= 0) continue;
    byPath.set(entry.path, entry);
    lines.push({ frames: entry.path.split(";"), value });
  }

  const layout = buildTree({ lines, total: 0, malformed: 0 }, rootName);

  // Second pass: sum each run's own value into every node of the shared tree.
  // The layout tree already has the shape, so this only has to attach numbers
  // to it -- and it has to do so for *both* runs even where one contributed
  // nothing, which is exactly the case the colouring is about.
  const attach = (node: FlameNode, prefix: string[]): DiffNode => {
    const path = [...prefix, node.name];
    const children = node.children.map((child) => attach(child, path));
    // A path may be both an interior node and a leaf: `A;B` can have samples
    // of its own and also appear as the parent of `A;B;C`.
    const self = byPath.get(path.slice(1).join(";"));
    let a = self ? self.a : 0;
    let b = self ? self.b : 0;
    for (const child of children) {
      a += child.a;
      b += child.b;
    }
    return { ...node, children, a, b, delta: b - a };
  };

  const root = attach(layout, []);
  // The root's own name is not part of any path, so it collects nothing
  // directly; its totals are the sums of its children, which `attach` has
  // already done.
  return root;
}

// -- colour -----------------------------------------------------------------

/**
 * Red grew, blue shrank, grey did not move.
 *
 * Intensity is the change *relative to this frame*, not relative to the
 * profile: a frame that doubled is fully saturated whether it is 40% of the
 * run or 0.4%. Scaling by the profile-wide maximum instead would leave every
 * frame but the one biggest mover a nearly identical pale wash, which is the
 * failure mode of most differential flame graphs. Width already carries "how
 * much of the profile is this"; colour is free to carry "how much did it
 * change".
 */
export function diffColour(node: { a: number; b: number; delta: number }): string {
  const scale = node.a + node.b;
  if (scale <= 0) return "hsl(220 12% 40%)";
  // delta / (a + b) lands in [-1, 1]: +1 is new in B, -1 is gone from B, and
  // a doubling (a -> 2a) gives +1/3, which is already a strong colour.
  const signed = node.delta / scale;
  const magnitude = Math.min(1, Math.abs(signed) * 3);
  if (magnitude < 0.06) return "hsl(220 10% 46%)";
  const hue = signed > 0 ? 4 : 212;
  const saturation = 20 + magnitude * 68;
  const lightness = 62 - magnitude * 22;
  return `hsl(${hue} ${saturation.toFixed(0)}% ${lightness.toFixed(0)}%)`;
}

/** Format a comparison value: a percentage when normalised, a count when not. */
export function formatValue(value: number, normalised: boolean): string {
  if (!normalised) return Math.round(value).toLocaleString();
  const pct = Math.abs(value) * 100;
  const sign = value < 0 ? "−" : "";
  // Rounding a nonzero share to "0.00%" would say the path is not there.
  if (pct !== 0 && pct < 0.01) return `${sign}<0.01%`;
  return `${sign}${pct.toFixed(2)}%`;
}

/** Format a signed delta with its sign always shown. */
export function formatDelta(value: number, normalised: boolean): string {
  const body = formatValue(Math.abs(value), normalised);
  if (value === 0) return normalised ? "0.00%" : "0";
  return `${value > 0 ? "+" : "−"}${body}`;
}

/** The last frame of a path, which is what a table wants in its narrow column. */
export function leafOf(path: string): string {
  const cut = path.lastIndexOf(";");
  return cut < 0 ? path : path.slice(cut + 1);
}
