/**
 * The Wakeups screen: who wakes whom.
 *
 * A thread that wakes three hundred others is a serialisation point whether
 * or not anybody designed it as one, and it is invisible in every other view
 * here: it uses almost no CPU, blocks for almost no time, and shows up in the
 * flame graph as a sliver. The only place it is obvious is in the shape of
 * the wakeup graph, which is why this screen is a picture rather than a table
 * — and why the table is underneath it anyway, because a picture is not
 * something you can quote in a bug report.
 *
 * The layout is a small force simulation run to convergence on load. Canvas,
 * for the same reason as the flame graph: a few thousand edges as DOM nodes
 * makes every frame a relayout.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Bundle } from "../bundle/load";
import { analyseWakeups, type WakeAnalysis, type WakeNode } from "../analysis";
import { Empty, Panel } from "../components/ui";

const WIDTH = 900;
const HEIGHT = 520;
const ITERATIONS = 320;

interface Placed {
  node: WakeNode;
  x: number;
  y: number;
  r: number;
  /** Force accumulated this iteration. Carried on the node rather than in a
   *  parallel array so the simulation reads as physics rather than indices. */
  fx: number;
  fy: number;
}

export function Wakeups({ bundle }: { bundle: Bundle }) {
  const analysis = useMemo(() => analyseWakeups(bundle), [bundle]);
  const [minCount, setMinCount] = useState(0);
  const [hover, setHover] = useState<Placed | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const view = useMemo(
    () => (analysis ? layout(analysis, minCount) : null),
    [analysis, minCount],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !view) return;
    draw(canvas, view, hover);
  }, [view, hover]);

  if (!analysis || analysis.edges.length === 0) {
    return (
      <Empty>
        This run has no wakeup graph. The <code>wakeup</code> probe runs in the
        <code> deep</code> profile only — it traces every{" "}
        <code>sched_wakeup</code>, which is the most expensive thing this tool
        does.
      </Empty>
    );
  }
  if (!view) return <Empty>Could not lay out the graph.</Empty>;

  const { placed, hidden } = view;

  return (
    <div className="space-y-4">
      <Panel
        title="Wakeup graph"
        right={
          <label className="flex items-center gap-2 text-xs text-slate-400">
            hide edges below
            <select
              aria-label="minimum wakeup count"
              value={minCount}
              onChange={(event) => setMinCount(Number(event.target.value))}
              className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            >
              {[0, 10, 100, 1000].map((value) => (
                <option key={value} value={value}>
                  {value === 0 ? "show all" : `${value.toLocaleString()} wakeups`}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {analysis.hub && analysis.hubShare >= 0.5 ? (
          <p className="text-sm text-slate-300">
            <span className="text-amber-300">One thread drives the process.</span>{" "}
            <code className="text-slate-100">{analysis.hub.name}</code> is
            responsible for{" "}
            <span className="tabular-nums text-slate-100">
              {(analysis.hubShare * 100).toFixed(0)}%
            </span>{" "}
            of all wakeups and wakes{" "}
            <span className="tabular-nums text-slate-100">
              {analysis.hub.fanOut.toLocaleString()}
            </span>{" "}
            distinct threads. Everything downstream of it is serialised behind
            whatever it does between wakeups.
          </p>
        ) : (
          <p className="text-sm text-slate-300">
            No single thread dominates the wakeups: the busiest waker accounts
            for{" "}
            <span className="tabular-nums text-slate-100">
              {(analysis.hubShare * 100).toFixed(0)}%
            </span>{" "}
            of {analysis.totalWakes.toLocaleString()} wakeups.
          </p>
        )}
        <p className="mt-1 text-xs text-slate-500">
          Node size is how many wakeups a thread sends; an arrow runs from the
          waker to the woken. {analysis.nodes.length.toLocaleString()} threads,{" "}
          {analysis.edges.length.toLocaleString()} edges
          {hidden > 0 && <> · {hidden.toLocaleString()} edges hidden by the filter</>}
          {analysis.truncated && (
            <>
              {" "}
              ·{" "}
              <span className="text-amber-300">
                the probe truncated its edge list
              </span>
              , so this graph is the busiest part of a bigger one
            </>
          )}
        </p>

        <div className="relative mt-3">
          <canvas
            ref={canvasRef}
            className="block w-full rounded bg-slate-950"
            style={{ aspectRatio: `${WIDTH} / ${HEIGHT}` }}
            onMouseMove={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              const scale = WIDTH / rect.width;
              const x = (event.clientX - rect.left) * scale;
              const y = (event.clientY - rect.top) * scale;
              setHover(nodeAt(placed, x, y));
            }}
            onMouseLeave={() => setHover(null)}
          />
          {hover && (
            <div
              className="pointer-events-none absolute z-10 rounded border border-slate-600 bg-slate-900/97 px-2 py-1 text-xs shadow-lg"
              style={{
                left: `${(hover.x / WIDTH) * 100}%`,
                top: `${(hover.y / HEIGHT) * 100}%`,
              }}
            >
              <div className="font-mono text-slate-100">{hover.node.name}</div>
              <div className="tabular-nums text-slate-400">
                tid {hover.node.tid} · wakes {hover.node.out.toLocaleString()} ·
                woken {hover.node.in.toLocaleString()} · fan-out{" "}
                {hover.node.fanOut.toLocaleString()}
              </div>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Wakers">
        <div className="max-h-80 overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">thread</th>
                <th className="py-1 pr-3 text-right">tid</th>
                <th className="py-1 pr-3 text-right">wakes sent</th>
                <th className="py-1 pr-3 text-right" title="distinct threads woken">
                  fan-out
                </th>
                <th className="py-1 text-right">wakes received</th>
              </tr>
            </thead>
            <tbody>
              {analysis.nodes.slice(0, 100).map((node) => (
                <tr key={node.tid} className="border-t border-slate-800">
                  <td className="py-1 pr-3 font-mono text-slate-100">
                    {node.name}
                    {node.external && (
                      <span
                        className="ml-2 text-xs text-slate-500"
                        title="not in this process's thread inventory: another process, or the kernel"
                      >
                        external
                      </span>
                    )}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
                    {node.tid}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-200">
                    {node.out.toLocaleString()}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                    {node.fanOut.toLocaleString()}
                  </td>
                  <td className="py-1 text-right tabular-nums text-slate-400">
                    {node.in.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

// -- layout -----------------------------------------------------------------

interface View {
  placed: Placed[];
  edges: { from: Placed; to: Placed; count: number }[];
  hidden: number;
}

/**
 * A force-directed layout, run to a fixed number of iterations.
 *
 * Deterministic on purpose: the initial positions come from the node's rank
 * rather than from `Math.random`, so the same run always draws the same
 * picture. A graph that rearranges itself every time it is opened cannot be
 * compared with the one in yesterday's bug report.
 */
function layout(analysis: WakeAnalysis, minCount: number): View {
  const kept = analysis.edges.filter((edge) => edge.count >= minCount);
  const hidden = analysis.edges.length - kept.length;

  const active = new Set<number>();
  for (const edge of kept) {
    active.add(edge.from);
    active.add(edge.to);
  }
  const nodes = analysis.nodes.filter((node) => active.has(node.tid));
  if (!nodes.length) return { placed: [], edges: [], hidden };

  const maxOut = Math.max(1, ...nodes.map((node) => node.out));
  const placed: Placed[] = nodes.map((node, index) => {
    // Seed on a spiral by rank: the busiest waker starts in the middle, which
    // is both a good starting guess and what makes the layout reproducible.
    const angle = index * 2.399963; // the golden angle, so the seeds spread
    const radius = (Math.sqrt(index) / Math.sqrt(nodes.length)) * (HEIGHT * 0.42);
    return {
      node,
      x: WIDTH / 2 + Math.cos(angle) * radius,
      y: HEIGHT / 2 + Math.sin(angle) * radius,
      r: 4 + Math.sqrt(node.out / maxOut) * 18,
      fx: 0,
      fy: 0,
    };
  });

  const index = new Map(placed.map((entry) => [entry.node.tid, entry]));
  const links = kept
    .map((edge) => ({
      from: index.get(edge.from) as Placed,
      to: index.get(edge.to) as Placed,
      count: edge.count,
    }))
    .filter((link) => link.from && link.to && link.from !== link.to);

  const maxCount = Math.max(1, ...links.map((link) => link.count));
  const area = WIDTH * HEIGHT;
  const k = Math.sqrt(area / placed.length) * 0.55;

  for (let step = 0; step < ITERATIONS; step += 1) {
    const cooling = 1 - step / ITERATIONS;
    for (const entry of placed) {
      entry.fx = 0;
      entry.fy = 0;
    }

    // Repulsion. O(n^2), which is fine: the probe caps the edge list at a few
    // thousand and the graph stops being readable long before that.
    for (let i = 0; i < placed.length; i += 1) {
      const a = placed[i] as Placed;
      for (let j = i + 1; j < placed.length; j += 1) {
        const b = placed[j] as Placed;
        let ex = a.x - b.x;
        let ey = a.y - b.y;
        let distance = Math.hypot(ex, ey);
        if (distance < 0.01) {
          // Two nodes exactly on top of each other have no direction to push
          // apart in; nudge them by their index so it stays deterministic.
          ex = ((i % 7) - 3) / 10;
          ey = ((j % 7) - 3) / 10;
          distance = Math.hypot(ex, ey) || 0.1;
        }
        const force = (k * k) / distance;
        const fx = (ex / distance) * force;
        const fy = (ey / distance) * force;
        a.fx += fx;
        a.fy += fy;
        b.fx -= fx;
        b.fy -= fy;
      }
    }

    // Attraction along edges, weighted by how much traffic they carry: a
    // thread woken ten thousand times sits close to its waker.
    for (const link of links) {
      const ex = link.from.x - link.to.x;
      const ey = link.from.y - link.to.y;
      const distance = Math.max(0.01, Math.hypot(ex, ey));
      const weight = 0.4 + (link.count / maxCount) * 1.6;
      const force = ((distance * distance) / k) * weight;
      const fx = (ex / distance) * force;
      const fy = (ey / distance) * force;
      link.from.fx -= fx;
      link.from.fy -= fy;
      link.to.fx += fx;
      link.to.fy += fy;
    }

    for (const entry of placed) {
      const move = Math.hypot(entry.fx, entry.fy);
      if (move > 0) {
        // Cap the step by the cooling schedule, or the first few iterations
        // fling everything into the margins and never recover.
        const limit = Math.min(move, k * cooling);
        entry.x += (entry.fx / move) * limit;
        entry.y += (entry.fy / move) * limit;
      }
      const margin = entry.r + 6;
      entry.x = Math.min(WIDTH - margin, Math.max(margin, entry.x));
      entry.y = Math.min(HEIGHT - margin, Math.max(margin, entry.y));
    }
  }

  return { placed, edges: links, hidden };
}

function nodeAt(placed: Placed[], x: number, y: number): Placed | null {
  let best: Placed | null = null;
  let bestDistance = Infinity;
  for (const entry of placed) {
    const distance = Math.hypot(entry.x - x, entry.y - y);
    if (distance <= entry.r + 3 && distance < bestDistance) {
      best = entry;
      bestDistance = distance;
    }
  }
  return best;
}

// -- drawing ----------------------------------------------------------------

function draw(canvas: HTMLCanvasElement, view: View, hover: Placed | null) {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = WIDTH * ratio;
  canvas.height = HEIGHT * ratio;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, WIDTH, HEIGHT);

  const maxCount = Math.max(1, ...view.edges.map((edge) => edge.count));
  for (const edge of view.edges) {
    const weight = edge.count / maxCount;
    const touched =
      hover && (edge.from === hover || edge.to === hover);
    ctx.strokeStyle = touched
      ? "rgba(250, 204, 21, 0.85)"
      : `rgba(56, 189, 248, ${0.10 + weight * 0.45})`;
    ctx.lineWidth = touched ? 2 : 0.5 + weight * 2.5;
    arrow(ctx, edge.from, edge.to);
  }

  for (const entry of view.placed) {
    const dimmed = hover && entry !== hover;
    ctx.beginPath();
    ctx.arc(entry.x, entry.y, entry.r, 0, Math.PI * 2);
    ctx.fillStyle = entry.node.external
      ? "rgba(148, 163, 184, 0.55)"
      : dimmed
        ? "rgba(251, 146, 60, 0.35)"
        : "rgba(251, 146, 60, 0.9)";
    ctx.fill();
    ctx.strokeStyle = "rgba(15, 23, 42, 0.9)";
    ctx.lineWidth = 1;
    ctx.stroke();

    // Only the nodes big enough to matter get a label; the rest is a tooltip
    // away, and a graph captioned three hundred times is a grey rectangle.
    if (entry.r > 9 || entry === hover) {
      ctx.fillStyle = "rgba(226, 232, 240, 0.95)";
      ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.fillText(entry.node.name, entry.x, entry.y - entry.r - 4);
    }
  }
}

/** A line from waker to woken, with a head so the direction is readable. */
function arrow(ctx: CanvasRenderingContext2D, from: Placed, to: Placed) {
  const angle = Math.atan2(to.y - from.y, to.x - from.x);
  const startX = from.x + Math.cos(angle) * from.r;
  const startY = from.y + Math.sin(angle) * from.r;
  const endX = to.x - Math.cos(angle) * (to.r + 2);
  const endY = to.y - Math.sin(angle) * (to.r + 2);

  ctx.beginPath();
  ctx.moveTo(startX, startY);
  ctx.lineTo(endX, endY);
  ctx.stroke();

  const head = 6;
  ctx.beginPath();
  ctx.moveTo(endX, endY);
  ctx.lineTo(
    endX - Math.cos(angle - Math.PI / 7) * head,
    endY - Math.sin(angle - Math.PI / 7) * head,
  );
  ctx.moveTo(endX, endY);
  ctx.lineTo(
    endX - Math.cos(angle + Math.PI / 7) * head,
    endY - Math.sin(angle + Math.PI / 7) * head,
  );
  ctx.stroke();
}
