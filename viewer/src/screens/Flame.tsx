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
  threadTotals,
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
  // Merged by default: with 315 roots the per thread view is a comb of
  // one-pixel slivers, and "where does the time go" is the first question
  // anyone asks. Which thread comes second, and is one click away.
  const [mergeThreads, setMergeThreads] = useState(true);

  const kind = available.find((entry) => entry.id === kindId) ?? available[0];
  const deferredFilter = useDeferredValue(threadFilter);
  const deferredSearch = useDeferredValue(searchTerm);

  const parsed = useMemo(() => {
    if (!kind) return null;
    const text = readText(bundle, kind.path);
    return text ? parseFolded(text) : null;
  }, [bundle, kind]);

  const threads = useMemo(
    () => (parsed ? threadTotals(parsed) : []),
    [parsed],
  );

  const tree = useMemo(() => {
    if (!parsed) return null;
    const filtered = filterLines(parsed, {
      threadFilter: deferredFilter,
      hideBelowShare,
      mergeThreads,
    });
    return { root: buildTree(filtered), filtered };
  }, [parsed, deferredFilter, hideBelowShare, mergeThreads]);

  if (!available.length) {
    return (
      <Empty>
        This run has no stack files. Only profiles that include a stack probe
        produce them — check the Overview for which probes ran.
      </Empty>
    );
  }
  if (!parsed || !tree || !kind) return <Empty>Could not read the stacks.</Empty>;

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
                  entry.id === kindId
                    ? "bg-sky-600 text-white"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>

          <label className="flex flex-col gap-1 text-xs text-slate-400">
            thread name
            <input
              value={threadFilter}
              onChange={(event) => setThreadFilter(event.target.value)}
              placeholder="e.g. worker"
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
            idle threads
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
          {threads.length.toLocaleString()} threads in this profile
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
      </Panel>

      <Panel title={`${kind.label} — ${kind.unit}`}>
        {tree.filtered.lines.length === 0 ? (
          <Empty>No stacks match these filters.</Empty>
        ) : (
          <FlameGraph
            root={tree.root}
            unit={kind.unit}
            searchTerm={deferredSearch}
            inverted={inverted}
          />
        )}
      </Panel>

      <Panel title="Threads in this profile">
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">Thread</th>
                <th className="py-1 pr-3 text-right">{kind.unit}</th>
                <th className="py-1 pr-3 text-right">Share</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {threads.slice(0, 200).map((thread) => (
                <tr key={thread.name} className="border-t border-slate-800">
                  <td className="py-1 pr-3 font-mono text-slate-200">{thread.name}</td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-300">
                    {thread.value.toLocaleString()}
                  </td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                    {(thread.share * 100).toFixed(2)}%
                  </td>
                  <td className="py-1 text-right">
                    <button
                      type="button"
                      onClick={() => setThreadFilter(thread.name)}
                      className="text-xs text-sky-400 hover:text-sky-300"
                    >
                      focus
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {threads.length > 200 && (
            <p className="py-2 text-center text-xs text-slate-500">
              showing the 200 busiest of {threads.length.toLocaleString()} threads
            </p>
          )}
        </div>
      </Panel>
    </div>
  );
}
