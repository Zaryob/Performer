/**
 * The Diff screen: two runs, one picture.
 *
 * This is the screen the project is actually for. Nobody profiles a process
 * once; they profile it before a change and after, or under light load and
 * under the load that hurts, and the finding is the difference. Everything
 * here is arranged around making that difference hard to misread:
 *
 *   * both runs are expressed as a share of their own total by default, so a
 *     longer run does not appear to have got worse at everything;
 *   * the paths that appeared and the paths that vanished get their own
 *     lists, because "this call path did not exist before" is a stronger
 *     statement than any percentage and gets lost in a sorted table;
 *   * a run whose stacks could not be resolved is refused as a comparand
 *     rather than drawn -- diffing noise against noise produces a confident
 *     picture of nothing.
 */

import { useDeferredValue, useMemo, useState } from "react";
import type { Bundle } from "../bundle/load";
import { readText, runDuration } from "../bundle/load";
import { parseFolded, type FlameNode } from "../bundle/folded";
import {
  buildDiffTree,
  diffColour,
  diffFolded,
  formatDelta,
  formatValue,
  leafOf,
  WIDTH_BASES,
  type DiffNode,
  type DiffResult,
  type PathDelta,
  type WidthBasis,
} from "../bundle/diff";
import { STACK_KINDS, type StackKindId } from "../bundle/types";
import { stacksAreTrustworthy } from "../quality";
import { FlameGraph } from "../components/FlameGraph";
import { Empty, Panel, formatDuration } from "../components/ui";

const MIN_SHARE_CHOICES = [
  { label: "every path", share: 0 },
  { label: "ignore below 0.01%", share: 0.0001 },
  { label: "ignore below 0.1%", share: 0.001 },
  { label: "ignore below 1%", share: 0.01 },
];

const BASIS_LABEL: Record<WidthBasis, string> = {
  both: "width: A + B (nothing hidden)",
  after: "width: B only (after)",
  before: "width: A only (before)",
};

const MOVERS = 25;

export interface DiffProps {
  bundles: Bundle[];
  aKey: string | null;
  bKey: string | null;
  onPick: (side: "a" | "b", key: string) => void;
  onSwap: () => void;
}

export function Diff({ bundles, aKey, bKey, onPick, onSwap }: DiffProps) {
  const a = bundles.find((bundle) => bundle.key === aKey) ?? null;
  const b = bundles.find((bundle) => bundle.key === bKey) ?? null;

  return (
    <div className="space-y-4">
      <Panel
        title="Compare"
        right={
          <button
            type="button"
            onClick={onSwap}
            disabled={!a || !b}
            className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600 disabled:opacity-40"
          >
            swap A ⇄ B
          </button>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <RunPicker
            label="A — before"
            aria="baseline run A"
            bundles={bundles}
            value={aKey}
            other={bKey}
            onPick={(key) => onPick("a", key)}
          />
          <RunPicker
            label="B — after"
            aria="comparison run B"
            bundles={bundles}
            value={bKey}
            other={aKey}
            onPick={(key) => onPick("b", key)}
          />
        </div>
      </Panel>

      {a && b && a.key !== b.key ? (
        <Comparison a={a} b={b} />
      ) : (
        <Empty>
          {bundles.length < 2
            ? "Load a second run bundle on the Runs screen — a diff needs two."
            : "Pick two different runs to compare."}
        </Empty>
      )}
    </div>
  );
}

function RunPicker({
  label,
  aria,
  bundles,
  value,
  other,
  onPick,
}: {
  label: string;
  aria: string;
  bundles: Bundle[];
  value: string | null;
  other: string | null;
  onPick: (key: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-400">
      {label}
      <select
        aria-label={aria}
        value={value ?? ""}
        onChange={(event) => onPick(event.target.value)}
        className="rounded border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100"
      >
        <option value="">— none —</option>
        {bundles.map((bundle) => (
          <option key={bundle.key} value={bundle.key}>
            {bundle.manifest.label}
            {bundle.key === other ? " (already the other side)" : ""} ·{" "}
            {bundle.manifest.run_id}
          </option>
        ))}
      </select>
    </label>
  );
}

// -- the comparison proper --------------------------------------------------

function Comparison({ a, b }: { a: Bundle; b: Bundle }) {
  const shared = STACK_KINDS.filter(
    (kind) => a.files.has(kind.path) && b.files.has(kind.path),
  );
  const [kindId, setKindId] = useState<StackKindId>(
    (shared[0]?.id ?? "oncpu") as StackKindId,
  );
  // Two runs of different lengths are the normal case, and comparing their
  // raw counts is the classic way to read a diff backwards, so normalisation
  // starts on whenever the durations disagree at all.
  const durationsDiffer =
    Math.abs(runDuration(a.manifest) - runDuration(b.manifest)) > 0.5;
  const [normalise, setNormalise] = useState(true);
  const [mergeThreads, setMergeThreads] = useState(true);
  const [threadFilter, setThreadFilter] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [minShare, setMinShare] = useState(0.0001);
  const [basis, setBasis] = useState<WidthBasis>("both");

  const kind = shared.find((entry) => entry.id === kindId) ?? shared[0];
  const deferredFilter = useDeferredValue(threadFilter);
  const deferredSearch = useDeferredValue(searchTerm);

  const diff = useMemo<DiffResult | null>(() => {
    if (!kind) return null;
    const left = readText(a, kind.path);
    const right = readText(b, kind.path);
    if (left === null || right === null) return null;
    return diffFolded(parseFolded(left), parseFolded(right), {
      normalise,
      mergeThreads,
      threadFilter: deferredFilter,
      minShare,
    });
  }, [a, b, kind, normalise, mergeThreads, deferredFilter, minShare]);

  const tree = useMemo(
    () => (diff ? buildDiffTree(diff, basis) : null),
    [diff, basis],
  );

  if (!shared.length) {
    return (
      <Empty>
        These two runs have no stack file in common, so there is nothing to
        diff. Check the Overview for which probes ran in each — comparing a
        <code> light</code> profile against a <code>deep</code> one usually
        means one of them never collected stacks.
      </Empty>
    );
  }
  if (!diff || !tree || !kind) return <Empty>Could not read the stacks.</Empty>;

  const problems = comparability(a, b);
  const untrustworthy = [a, b].filter(
    (bundle) => !stacksAreTrustworthy(bundle.manifest),
  );

  return (
    <div className="space-y-4">
      {untrustworthy.length > 0 && (
        <div className="rounded border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          <span className="mr-2 font-bold">✕</span>
          {untrustworthy.length === 2 ? "Both runs have" : "One run has"} stacks
          that could not be resolved (
          {untrustworthy
            .map(
              (bundle) =>
                `${bundle.manifest.label}: ${(
                  bundle.manifest.quality.unknown_frame_ratio * 100
                ).toFixed(0)}% [unknown]`,
            )
            .join(", ")}
          ). A diff of unresolved stacks is a confident picture of nothing —
          every difference below is between two sets of truncated call paths.
          Rebuild with <code>-fno-omit-frame-pointer</code> and measure again.
        </div>
      )}
      {problems.map((problem) => (
        <div
          key={problem}
          className="rounded border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-100"
        >
          <span className="mr-2 font-bold">!</span>
          {problem}
        </div>
      ))}

      <Panel title="Comparison">
        <div className="flex flex-wrap items-end gap-3">
          {shared.length > 1 && (
            <div className="flex gap-1">
              {shared.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setKindId(entry.id)}
                  className={`rounded px-3 py-1.5 text-sm ${
                    entry.id === kindId
                      ? "bg-sky-600 text-white"
                      : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                  }`}
                >
                  {entry.label}
                </button>
              ))}
            </div>
          )}

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            thread name
            <input
              value={threadFilter}
              onChange={(event) => setThreadFilter(event.target.value)}
              placeholder="e.g. worker"
              className="w-40 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            search frames
            <input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="e.g. __lll_lock_wait"
              className="w-52 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            noise floor
            <select
              aria-label="noise floor"
              value={minShare}
              onChange={(event) => setMinShare(Number(event.target.value))}
              className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            >
              {MIN_SHARE_CHOICES.map((choice) => (
                <option key={choice.share} value={choice.share}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            layout
            <select
              aria-label="layout basis"
              value={basis}
              onChange={(event) => setBasis(event.target.value as WidthBasis)}
              className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            >
              {WIDTH_BASES.map((option) => (
                <option key={option} value={option}>
                  {BASIS_LABEL[option]}
                </option>
              ))}
            </select>
          </label>

          <div className="flex flex-col gap-1">
            <label
              className="flex items-center gap-2 text-xs text-slate-300"
              title="Express each run as a share of its own total, so a longer run does not appear to have grown everywhere"
            >
              <input
                type="checkbox"
                checked={normalise}
                onChange={(event) => setNormalise(event.target.checked)}
              />
              normalise
              {durationsDiffer && !normalise && (
                <span className="text-amber-300">← durations differ</span>
              )}
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={mergeThreads}
                onChange={(event) => setMergeThreads(event.target.checked)}
              />
              merge threads
            </label>
          </div>
        </div>

        <Verdict a={a} b={b} kindUnit={kind.unit} diff={diff} />
      </Panel>

      <Panel
        title={`${kind.label} — what changed`}
        right={<Legend />}
      >
        {tree.value <= 0 ? (
          <Empty>No paths survive these filters.</Empty>
        ) : (
          <FlameGraph
            root={tree}
            unit={kind.unit}
            searchTerm={deferredSearch}
            colourFor={(node, highlighted) =>
              highlighted ? "#8b5cf6" : diffColour(node as DiffNode)
            }
            describe={(node) => <NodeNumbers node={node} diff={diff} />}
            summary={
              <span>
                <span className="text-rose-300 tabular-nums">
                  +{(diff.grew * (diff.normalised ? 100 : 1)).toFixed(
                    diff.normalised ? 2 : 0,
                  )}
                  {diff.normalised ? "%" : ""}
                </span>{" "}
                grew ·{" "}
                <span className="text-sky-300 tabular-nums">
                  −{(diff.shrank * (diff.normalised ? 100 : 1)).toFixed(
                    diff.normalised ? 2 : 0,
                  )}
                  {diff.normalised ? "%" : ""}
                </span>{" "}
                shrank
              </span>
            }
          />
        )}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title={`Biggest movers (top ${MOVERS})`}>
          <MoverTable rows={diff.paths.slice(0, MOVERS)} diff={diff} />
        </Panel>
        <div className="space-y-4">
          <Panel
            title={`New in B — ${b.manifest.label} (${diff.onlyInB.length})`}
          >
            <ExclusiveList
              rows={diff.onlyInB}
              diff={diff}
              side="b"
              empty="Every call path in B was also present in A."
            />
          </Panel>
          <Panel title={`Gone from B (${diff.onlyInA.length})`}>
            <ExclusiveList
              rows={diff.onlyInA}
              diff={diff}
              side="a"
              empty="Every call path in A is still present in B."
            />
          </Panel>
        </div>
      </div>
    </div>
  );
}

// -- header pieces ----------------------------------------------------------

/**
 * Reasons two runs may not be comparable.
 *
 * Not errors: an operator may well want to compare a run against a rebuilt
 * binary, and refusing would be worse than saying so. But each of these makes
 * a difference in the graph mean something other than "the workload changed",
 * and finding that out afterwards is expensive.
 */
function comparability(a: Bundle, b: Bundle): string[] {
  const problems: string[] = [];
  if (a.manifest.profile !== b.manifest.profile) {
    problems.push(
      `Different collection profiles (${a.manifest.profile} vs ${b.manifest.profile}). ` +
        "Different probes sample at different rates, so some of the difference below is the tooling.",
    );
  }
  if (a.manifest.target.comm !== b.manifest.target.comm) {
    problems.push(
      `Different target processes (${a.manifest.target.comm} vs ${b.manifest.target.comm}).`,
    );
  }
  for (const bundle of [a, b]) {
    if (bundle.manifest.status !== "ok") {
      problems.push(
        `${bundle.manifest.label} is a ${bundle.manifest.status} run — some of its probes did not finish, ` +
          "so a path missing from it may be missing data rather than missing work.",
      );
    }
  }
  const before = a.manifest.quality.estimated_overhead_pct;
  const after = b.manifest.quality.estimated_overhead_pct;
  if (Math.abs(before - after) > 10) {
    problems.push(
      `Measurement overhead differed a lot between the runs (${before.toFixed(0)}% vs ${after.toFixed(0)}%). ` +
        "Part of what changed may be the cost of measuring.",
    );
  }
  return problems;
}

function Verdict({
  a,
  b,
  kindUnit,
  diff,
}: {
  a: Bundle;
  b: Bundle;
  kindUnit: string;
  diff: DiffResult;
}) {
  const moved = diff.normalised
    ? `${(diff.grew * 100).toFixed(1)}% of the profile`
    : `${Math.round(diff.grew).toLocaleString()} ${kindUnit}`;
  return (
    <div className="mt-3 space-y-1 text-xs text-slate-400">
      <p>
        <span className="text-slate-200">A</span> {a.manifest.label} ·{" "}
        {formatDuration(runDuration(a.manifest))} ·{" "}
        <span className="tabular-nums">
          {diff.rawTotalA.toLocaleString()} {kindUnit}
        </span>
        {"   →   "}
        <span className="text-slate-200">B</span> {b.manifest.label} ·{" "}
        {formatDuration(runDuration(b.manifest))} ·{" "}
        <span className="tabular-nums">
          {diff.rawTotalB.toLocaleString()} {kindUnit}
        </span>
      </p>
      <p>
        {diff.paths.length.toLocaleString()} call paths compared
        {diff.belowThreshold > 0 && (
          <> · {diff.belowThreshold.toLocaleString()} below the noise floor</>
        )}{" "}
        · {moved} moved into paths that grew
        {diff.normalised ? (
          <>
            {" "}
            · shares, so the {formatDuration(runDuration(a.manifest))} and{" "}
            {formatDuration(runDuration(b.manifest))} runs are on the same scale
          </>
        ) : (
          <> · raw {kindUnit}, not normalised</>
        )}
      </p>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex items-center gap-3 text-xs text-slate-400">
      <span className="flex items-center gap-1">
        <span
          className="inline-block h-3 w-4 rounded-sm"
          style={{ background: diffColour({ a: 1, b: 3, delta: 2 }) }}
        />
        grew in B
      </span>
      <span className="flex items-center gap-1">
        <span
          className="inline-block h-3 w-4 rounded-sm"
          style={{ background: diffColour({ a: 3, b: 1, delta: -2 }) }}
        />
        shrank
      </span>
      <span className="flex items-center gap-1">
        <span
          className="inline-block h-3 w-4 rounded-sm"
          style={{ background: diffColour({ a: 1, b: 1, delta: 0 }) }}
        />
        unchanged
      </span>
    </div>
  );
}

function NodeNumbers({ node, diff }: { node: FlameNode; diff: DiffResult }) {
  const entry = node as DiffNode;
  const grew = entry.delta > 0;
  return (
    <>
      A {formatValue(entry.a, diff.normalised)} → B{" "}
      {formatValue(entry.b, diff.normalised)} ·{" "}
      <span className={grew ? "text-rose-300" : "text-sky-300"}>
        {formatDelta(entry.delta, diff.normalised)}
      </span>
      {entry.a > 0 && (
        <>
          {" "}
          ({grew ? "+" : "−"}
          {Math.abs((entry.delta / entry.a) * 100).toFixed(0)}%)
        </>
      )}
      {entry.a === 0 && <> · new</>}
      {entry.b === 0 && <> · gone</>}
    </>
  );
}

// -- tables -----------------------------------------------------------------

function PathCell({ path }: { path: string }) {
  const leaf = leafOf(path);
  return (
    <td className="max-w-0 py-1 pr-3" title={path.replace(/;/g, " → ")}>
      <div className="truncate font-mono text-slate-100">{leaf}</div>
      <div className="truncate text-xs text-slate-500">
        {path.slice(0, Math.max(0, path.length - leaf.length - 1)) || "(root)"}
      </div>
    </td>
  );
}

function MoverTable({ rows, diff }: { rows: PathDelta[]; diff: DiffResult }) {
  if (!rows.length) return <Empty>Nothing moved.</Empty>;
  return (
    <div className="max-h-[28rem] overflow-auto">
      <table className="w-full table-fixed text-sm">
        <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="py-1 pr-3">call path</th>
            <th className="w-20 py-1 pr-3 text-right">A</th>
            <th className="w-20 py-1 pr-3 text-right">B</th>
            <th className="w-24 py-1 pr-3 text-right">Δ</th>
            <th className="w-16 py-1 text-right">Δ%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.path} className="border-t border-slate-800">
              <PathCell path={row.path} />
              <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                {formatValue(row.a, diff.normalised)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                {formatValue(row.b, diff.normalised)}
              </td>
              <td
                className={`py-1 pr-3 text-right tabular-nums ${
                  row.delta > 0 ? "text-rose-300" : "text-sky-300"
                }`}
              >
                {formatDelta(row.delta, diff.normalised)}
              </td>
              <td className="py-1 text-right tabular-nums text-slate-500">
                {row.ratio === null
                  ? "new"
                  : `${row.ratio > 0 ? "+" : "−"}${Math.abs(row.ratio * 100).toFixed(0)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ExclusiveList({
  rows,
  diff,
  side,
  empty,
}: {
  rows: PathDelta[];
  diff: DiffResult;
  side: "a" | "b";
  empty: string;
}) {
  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className="max-h-56 overflow-auto">
      <table className="w-full table-fixed text-sm">
        <tbody>
          {rows.slice(0, 40).map((row) => (
            <tr key={row.path} className="border-t border-slate-800 first:border-0">
              <PathCell path={row.path} />
              <td
                className={`w-20 py-1 text-right tabular-nums ${
                  side === "b" ? "text-rose-300" : "text-sky-300"
                }`}
              >
                {formatValue(side === "b" ? row.b : row.a, diff.normalised)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 40 && (
        <p className="py-2 text-center text-xs text-slate-500">
          showing the 40 largest of {rows.length.toLocaleString()}
        </p>
      )}
    </div>
  );
}
