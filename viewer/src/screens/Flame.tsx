/**
 * The Flame screen.
 *
 * The filter controls are not a convenience. A pool of 315 threads, most of
 * them parked in an idle wait, produces a graph where that wait is the widest
 * thing on screen and everything worth seeing is a sliver. Filtering by thread
 * name and hiding the threads that did nothing is what makes the graph
 * readable at all.
 */

import { useDeferredValue, useMemo, useState } from "react";
import type { Bundle } from "../bundle/load";
import { readText } from "../bundle/load";
import {
  buildTree,
  filterLines,
  parseFolded,
  threadProfileCoverage,
} from "../bundle/folded";
import { STACK_KINDS, type StackKindId } from "../bundle/types";
import { stacksAreTrustworthy } from "../quality";
import { FlameGraph } from "../components/FlameGraph";
import { Empty, Panel } from "../components/ui";

const IDLE_CHOICES = [
  { label: "show all threads", share: 0 },
  { label: "hide below 0.1%", share: 0.001 },
  { label: "hide below 1%", share: 0.01 },
  { label: "hide below 5%", share: 0.05 },
];

export function Flame({ bundle }: { bundle: Bundle }) {
  const available = STACK_KINDS.filter((kind) => bundle.files.has(kind.path));
  const [kindId, setKindId] = useState<StackKindId>(
    (available[0]?.id ?? "oncpu") as StackKindId,
  );
  const [threadFilter, setThreadFilter] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [hideBelowShare, setHideBelowShare] = useState(0);
  const [inverted, setInverted] = useState(false);
  const [mergeThreads, setMergeThreads] = useState(false);

  const kind = available.find((entry) => entry.id === kindId) ?? available[0] ?? STACK_KINDS[0];
  const deferredFilter = useDeferredValue(threadFilter);
  const deferredSearch = useDeferredValue(searchTerm);

  const parsed = useMemo(() => {
    const text = readText(bundle, kind.path);
    return parseFolded(text ?? "");
  }, [bundle, kind]);

  const coverage = useMemo(
    () => threadProfileCoverage(parsed, bundle.threads),
    [parsed, bundle.threads],
  );
  const visibleThreads = useMemo(
    () => {
      const needle = deferredFilter.trim().toLowerCase();
      const tidFilter = /^\d+$/.test(needle) ? Number(needle) : null;
      return coverage.rows.filter((row) => tidFilter !== null ? row.tid === tidFilter
        : !needle ||
          `${row.name} ${row.tid === null ? "" : `[tid=${row.tid}]`} ${row.roots.join(" ")}`
            .toLowerCase().includes(needle));
    },
    [coverage, deferredFilter],
  );

  const tree = useMemo(() => {
    const filtered = filterLines(parsed, {
      threadFilter: deferredFilter,
      hideBelowShare,
      mergeThreads,
    });
    return { root: buildTree(filtered), filtered };
  }, [parsed, deferredFilter, hideBelowShare, mergeThreads]);

  const trustworthy = stacksAreTrustworthy(bundle.manifest);
  const hiddenValue = parsed.total - tree.filtered.total;

  return (
    <div className="space-y-4">
      {!trustworthy && (
        <div className="rounded border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          <span className="mr-2 font-bold">✕</span>
          {(bundle.manifest.quality.unknown_frame_ratio * 100).toFixed(0)}% of the
          frames in this run are <code>[unknown]</code>. This graph is drawn from
          stacks that could not be resolved, so it will look plausible and mean
          nothing. Rebuild the target with <code>-fno-omit-frame-pointer</code>.
        </div>
      )}

      <Panel
        title="Filters"
        right={
          <div className="flex items-center gap-4">
            <label
              className="flex items-center gap-2 text-xs text-slate-300"
              title="Drop the thread frame so the same call path from different threads combines"
            >
              <input
                type="checkbox"
                checked={mergeThreads}
                onChange={(event) => setMergeThreads(event.target.checked)}
              />
              merge threads
            </label>
            <label className="flex items-center gap-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={inverted}
                onChange={(event) => setInverted(event.target.checked)}
              />
              icicle (top down)
            </label>
          </div>
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex gap-1">
            {available.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setKindId(entry.id)}
                className={`rounded px-3 py-1.5 text-sm ${
                  entry.id === kind.id
                    ? "bg-sky-600 text-white"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            thread name or TID
            <input
              value={threadFilter}
              onChange={(event) => setThreadFilter(event.target.value)}
              placeholder="e.g. worker or 123"
              className="w-48 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            search frames
            <input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="e.g. pthread_mutex_lock"
              className="w-64 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            minimum stack share
            <select
              value={hideBelowShare}
              onChange={(event) => setHideBelowShare(Number(event.target.value))}
              className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
            >
              {IDLE_CHOICES.map((choice) => (
                <option key={choice.share} value={choice.share}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <p className="mt-3 text-xs text-slate-500">
          {bundle.threads ? `${coverage.inventoryThreads.toLocaleString()} threads in /proc inventory`
            : "/proc thread inventory unavailable"}
          {" · "}{coverage.recordedThreads.toLocaleString()} TIDs with recorded stacks
          {coverage.nameAggregates > 0 && (
            <> · {coverage.nameAggregates.toLocaleString()} legacy name aggregates</>
          )}
          {mergeThreads && " · merged into one tree"}
          {hiddenValue > 0 && (
            <>
              {" "}
              · filters hide{" "}
              <span className="text-amber-300">
                {((hiddenValue / parsed.total) * 100).toFixed(1)}%
              </span>{" "}
              of the total
            </>
          )}
          {parsed.malformed > 0 && (
            <> · {parsed.malformed} malformed lines skipped</>
          )}
        </p>
        {coverage.withoutStacks > 0 && available.length > 0 && (
          <p className="mt-2 text-xs text-amber-200">
            {coverage.withoutStacks.toLocaleString()} inventory threads have no {kind.label} stacks
            captured. On-CPU sampling only sees threads running at a sample; Off-CPU
            records completed blocked intervals. A thread parked throughout the capture
            may have no stack. Missing stacks do not establish zero CPU or blocked time;
            the Threads screen has the /proc counters.
          </p>
        )}
        {coverage.nameAggregates > 0 && (
          <p className="mt-2 text-xs text-amber-200">
            These older stacks combine threads with the same name. Their root count
            is not a thread count, and values cannot be assigned to individual TIDs.
            Capture again with the updated collector for separate thread roots.
          </p>
        )}
      </Panel>

      <Panel title={`${kind.label} — ${kind.unit}`}>
        {!available.length ? (
          <Empty>
            This run has no stack files. Check the Overview for which probes ran.
            Thread inventory below remains available without stack measurements.
          </Empty>
        ) : tree.filtered.lines.length === 0 ? (
          <Empty>{parsed.lines.length === 0 ? "This probe recorded no stacks." : "No stacks match these filters."}</Empty>
        ) : (
          <FlameGraph
            root={tree.root}
            unit={kind.unit}
            searchTerm={deferredSearch}
            inverted={inverted}
          />
        )}
      </Panel>

      <Panel title="Thread inventory and stack coverage">
        <p className="mb-2 text-xs text-slate-400">
          {visibleThreads.length.toLocaleString()} of {coverage.rows.length.toLocaleString()} rows
          {" · "}All matching threads are listed. A dash means no attributable stack measurement.
        </p>
        <div className="max-h-96 overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">Thread</th>
                <th className="py-1 pr-3 text-right">TID</th>
                <th className="py-1 pr-3 text-right">{kind.unit}</th>
                <th className="py-1 pr-3 text-right">Share</th>
                <th className="py-1 pr-3">Stack coverage</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {visibleThreads.map((thread) => (
                <tr key={thread.key} className="border-t border-slate-800">
                  <td className="py-1 pr-3 font-mono text-slate-200">{thread.name}</td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                    {thread.tid ?? "—"}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-300">
                    {thread.value?.toLocaleString() ?? "—"}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                    {thread.share === null ? "—" : `${(thread.share * 100).toFixed(2)}%`}
                  </td>
                  <td className="py-1 pr-3 text-xs text-slate-400">
                    {thread.coverage === "none" ? `no ${kind.label} stack captured`
                      : thread.coverage === "unidentified" ? "TID unavailable in legacy stacks"
                        : thread.coverage === "recorded" && !thread.inInventory ? "recorded · outside snapshots"
                          : thread.coverage}
                  </td>
                  <td className="py-1 text-right">
                    {thread.roots.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setThreadFilter(thread.tid === null ? thread.name : `[tid=${thread.tid}]`)}
                      className="text-xs text-sky-400 hover:text-sky-300"
                    >
                      focus
                    </button>
                    )}
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
