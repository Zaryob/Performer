/**
 * A flame graph, drawn on a canvas.
 *
 * Canvas rather than SVG because of the size of the problem: a profile of a
 * 315 thread process runs to tens of thousands of frames, and that many DOM
 * nodes takes seconds to lay out and makes every subsequent interaction
 * stutter. On a canvas the whole graph is one element, drawing is linear in
 * the number of *visible* frames, and hit testing is a lookup.
 *
 * Drawn by hand rather than with d3-flame-graph, which cannot express the
 * differential mode. That mode is not a second renderer: the Diff screen
 * passes `colourFor` and `describe` and gets the same picture with a
 * different meaning assigned to colour.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  flatten,
  frameColour,
  maxDepth,
  search,
  type FlameNode,
} from "../bundle/folded";

const ROW_HEIGHT = 18;
const MIN_DRAW_WIDTH = 0.4; // px; narrower frames cannot be seen or clicked
const FONT = "11px ui-monospace, SFMono-Regular, Menlo, monospace";

export interface FlameGraphProps {
  root: FlameNode;
  /** Unit of the values, for the tooltip and the header. */
  unit: string;
  searchTerm?: string;
  /** Icicle mode grows downward from the root, which suits deep stacks. */
  inverted?: boolean;
  /** Cap for very deep profiles; beyond this the graph scrolls. */
  maxHeight?: number;
  /**
   * Override the fill of each frame.
   *
   * This is the whole of what the differential mode needs from the renderer.
   * A flame graph and a differential flame graph are the same picture with
   * two different meanings assigned to colour, so the Diff screen passes its
   * red/blue ramp here rather than there being a second canvas renderer to
   * keep in step with this one. `highlighted` is the search match, which
   * still has to win: finding a frame by name matters more than reading its
   * delta at the moment you are looking for it.
   */
  colourFor?: (node: FlameNode, highlighted: boolean) => string;
  /** Replace the second line of the tooltip, where the numbers live. */
  describe?: (node: FlameNode) => ReactNode;
  /**
   * Replace the leading "N units" in the header. The differential graph's
   * root value is a layout basis rather than a measurement, and printing it
   * would be offering a number that means nothing.
   */
  summary?: ReactNode;
}

interface Hover {
  node: FlameNode;
  x: number;
  y: number;
}

export function FlameGraph({
  root,
  unit,
  searchTerm = "",
  inverted = false,
  maxHeight = 900,
  colourFor,
  describe,
  summary,
}: FlameGraphProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(1200);
  const [focus, setFocus] = useState<FlameNode>(root);
  const [hover, setHover] = useState<Hover | null>(null);

  useEffect(() => setFocus(root), [root]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(Math.max(320, Math.floor(entry.contentRect.width)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const nodes = useMemo(() => flatten(root), [root]);
  const depth = useMemo(() => maxDepth(root), [root]);
  const matches = useMemo(() => search(root, searchTerm), [root, searchTerm]);

  // Sized to the content rather than to a fixed height: a nine deep profile
  // in a 520px box is mostly empty space, and the graph reads as if something
  // failed to load.
  const rows = depth + 1;
  const canvasHeight = Math.min(maxHeight, rows * ROW_HEIGHT + 4);

  /** Frames outside the focused subtree are not drawn at all. */
  const scale = focus.value > 0 ? width / focus.value : 0;

  const geometry = useCallback(
    (node: FlameNode) => {
      const x = (node.start - focus.start) * scale;
      const w = node.value * scale;
      const row = node.depth - focus.depth;
      const y = inverted ? row * ROW_HEIGHT : canvasHeight - (row + 1) * ROW_HEIGHT;
      return { x, y, w };
    },
    [focus, scale, inverted, canvasHeight],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = canvasHeight * ratio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${canvasHeight}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, canvasHeight);
    ctx.font = FONT;
    ctx.textBaseline = "middle";

    for (const node of nodes) {
      if (node.depth < focus.depth) continue;
      const { x, y, w } = geometry(node);
      if (w < MIN_DRAW_WIDTH || x > width || x + w < 0) continue;

      const highlighted = matches.matches.has(node);
      ctx.fillStyle = colourFor
        ? colourFor(node, highlighted)
        : frameColour(node.name, highlighted);
      ctx.fillRect(x, y, Math.max(w - 1, 0.5), ROW_HEIGHT - 1);

      if (w > 26) {
        const label = fitText(ctx, node.name, w - 6);
        if (label) {
          ctx.fillStyle = "rgba(0,0,0,0.82)";
          ctx.fillText(label, x + 3, y + ROW_HEIGHT / 2);
        }
      }
    }
  }, [nodes, width, canvasHeight, geometry, matches, focus, colourFor]);

  const nodeAt = useCallback(
    (px: number, py: number): FlameNode | null => {
      for (const node of nodes) {
        if (node.depth < focus.depth) continue;
        const { x, y, w } = geometry(node);
        if (w < MIN_DRAW_WIDTH) continue;
        if (px >= x && px <= x + w && py >= y && py <= y + ROW_HEIGHT) return node;
      }
      return null;
    },
    [nodes, geometry, focus],
  );

  const onMove = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const node = nodeAt(px, py);
    setHover(node ? { node, x: px, y: py } : null);
  };

  const onClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const node = nodeAt(event.clientX - rect.left, event.clientY - rect.top);
    if (node && node.children.length) setFocus(node);
  };

  const total = root.value;
  const share = (value: number) => (total > 0 ? (value / total) * 100 : 0);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-400">
        {summary ?? (
          <span>
            <span className="text-slate-200 tabular-nums">
              {total.toLocaleString()}
            </span>{" "}
            {unit}
          </span>
        )}
        <span>
          {nodes.length.toLocaleString()} frames, {rows} deep
        </span>
        {searchTerm.trim() && (
          <span className="text-violet-300">
            matched {share(matches.matchedValue).toFixed(1)}% (
            {matches.matches.size.toLocaleString()} frames)
          </span>
        )}
        {focus !== root && (
          <button
            type="button"
            onClick={() => setFocus(root)}
            className="rounded bg-slate-700 px-2 py-0.5 text-slate-100 hover:bg-slate-600"
          >
            zoomed into <code>{focus.name}</code> — reset
          </button>
        )}
        {focus === root && <span className="text-slate-500">click a frame to zoom</span>}
      </div>

      <div ref={containerRef} className="relative w-full overflow-hidden">
        <canvas
          ref={canvasRef}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
          onClick={onClick}
          className="block cursor-pointer rounded bg-slate-950"
        />
        {hover && (
          <div
            className="pointer-events-none absolute z-10 max-w-xl rounded border border-slate-600 bg-slate-900/97 px-2 py-1 text-xs shadow-lg"
            style={{
              left: Math.min(hover.x + 12, Math.max(0, width - 420)),
              top: Math.max(0, hover.y - 44),
            }}
          >
            <div className="font-mono break-all text-slate-100">{hover.node.name}</div>
            <div className="text-slate-400 tabular-nums">
              {describe ? (
                describe(hover.node)
              ) : (
                <>
                  {hover.node.value.toLocaleString()} {unit} ·{" "}
                  {share(hover.node.value).toFixed(2)}% of total
                  {hover.node.self > 0 && (
                    <> · {hover.node.self.toLocaleString()} self</>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Trim a label to the width available, with an ellipsis when it does not fit. */
function fitText(
  ctx: CanvasRenderingContext2D,
  text: string,
  available: number,
): string {
  if (available <= 8) return "";
  if (ctx.measureText(text).width <= available) return text;
  // Binary search beats measuring character by character on the long C++
  // symbols this tool exists to display.
  let low = 0;
  let high = text.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= available) low = mid;
    else high = mid - 1;
  }
  return low > 1 ? `${text.slice(0, low)}…` : "";
}
