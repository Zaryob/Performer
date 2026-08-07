/**
 * The Locks screen.
 *
 * The acceptance test for this whole project is that on a program with one
 * known hot mutex, the tool puts that mutex first. So this screen has exactly
 * one job and it is a ranking — but a ranking of addresses would fail that
 * test in spirit while passing it in letter. `0x7f3e03018240` is an identity,
 * not an answer; nobody can act on it. The address is the row, and the code
 * that takes it is what the row says.
 *
 * A lock's cost is the *sum* of what every thread waited on it, so it can and
 * does exceed the wall clock several times over. Every number here is
 * therefore given per thread and per second as well, because "6.8 seconds of
 * waiting" in a 12 second run is either catastrophic or irrelevant depending
 * on a denominator the raw figure does not carry.
 */

import { useMemo, useState } from "react";
import type { Bundle } from "../bundle/load";
import { runDuration } from "../bundle/load";
import { analyseLocks, blameFrame, type Lock } from "../analysis";
import { Empty, Panel } from "../components/ui";

export function Locks({ bundle }: { bundle: Bundle }) {
  const analysis = useMemo(() => analyseLocks(bundle), [bundle]);
  const [openAddr, setOpenAddr] = useState<string | null>(null);

  if (!analysis || analysis.locks.length === 0) {
    return (
      <Empty>
        This run has no futex data. The <code>futex</code> probe runs in the
        <code> standard</code> and <code>deep</code> profiles — check the
        Overview for which probes delivered.
      </Empty>
    );
  }

  const seconds = runDuration(bundle.manifest);
  const threads = Math.max(1, bundle.manifest.target.thread_count_start);
  const worst = analysis.locks[0] as Lock;
  const single = worst.share >= 0.5;

  return (
    <div className="space-y-4">
      <Panel title="Contended locks">
        <p className="text-sm text-slate-300">
          {single ? (
            <>
              <span className="text-amber-300">One address dominates.</span>{" "}
              <code className="text-slate-100">{worst.addr}</code> accounts for{" "}
              <span className="text-slate-100 tabular-nums">
                {(worst.share * 100).toFixed(0)}%
              </span>{" "}
              of all futex wait time in this run
              {worst.sites.length > 0 && (
                <>
                  , and{" "}
                  <span className="text-slate-100 tabular-nums">
                    {(((worst.sites[0] as { totalUs: number }).totalUs / worst.totalUs) * 100).toFixed(0)}%
                  </span>{" "}
                  of that comes from{" "}
                  <code className="text-slate-100">
                    {blameFrame((worst.sites[0] as { stack: string }).stack)}
                  </code>
                </>
              )}
              . That is a single contended mutex, not diffuse lock traffic.
            </>
          ) : (
            <>
              No single address dominates: the hottest holds{" "}
              <span className="text-slate-100 tabular-nums">
                {(worst.share * 100).toFixed(0)}%
              </span>{" "}
              of futex wait time across {analysis.locks.length.toLocaleString()}{" "}
              addresses. Lock traffic here is spread, so there is no one mutex to fix.
            </>
          )}
        </p>
        <p className="mt-2 text-xs text-slate-500">
          Totals are summed over every thread, so they exceed the{" "}
          {seconds.toFixed(0)} s run. Addresses are meaningful only within this
          run — they are identities, not something to compare between runs.
          {!analysis.attributed && (
            <>
              {" "}
              <span className="text-amber-300">
                This bundle has no call-path attribution
              </span>{" "}
              (<code>hist/futex_sites.json</code> is absent), so the addresses
              are ranked but not explained.
            </>
          )}
          {analysis.truncated && (
            <> The table was truncated, so "no other lock matters" is unknown here, not established.</>
          )}
        </p>
      </Panel>

      <Panel title={`Locks by wait time (${analysis.locks.length.toLocaleString()})`}>
        <div className="max-h-[36rem] overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-1 pr-3">address</th>
                <th className="py-1 pr-3">taken by</th>
                <th className="py-1 pr-3 text-right">wait</th>
                <th className="py-1 pr-3 text-right">share</th>
                <th className="py-1 pr-3 text-right">waits</th>
                <th className="py-1 pr-3 text-right" title="mean time a thread spent blocked on this lock">
                  avg
                </th>
                <th
                  className="py-1 text-right"
                  title="wait time divided by the thread count and the run length"
                >
                  per thread
                </th>
              </tr>
            </thead>
            <tbody>
              {analysis.locks.map((lock) => {
                const open = openAddr === lock.addr;
                const perThread =
                  seconds > 0 ? lock.totalUs / 1e6 / threads / seconds : 0;
                return (
                  <LockRows
                    key={lock.addr}
                    lock={lock}
                    open={open}
                    perThread={perThread}
                    onToggle={() => setOpenAddr(open ? null : lock.addr)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function LockRows({
  lock,
  open,
  perThread,
  onToggle,
}: {
  lock: Lock;
  open: boolean;
  perThread: number;
  onToggle: () => void;
}) {
  const top = lock.sites[0];
  const expandable = lock.sites.length > 0;
  return (
    <>
      <tr
        className={`border-t border-slate-800 ${
          expandable ? "cursor-pointer hover:bg-slate-800/40" : ""
        }`}
        onClick={expandable ? onToggle : undefined}
      >
        <td className="py-1 pr-3 font-mono text-xs text-slate-300">
          {expandable && (
            <span className="mr-1 inline-block w-2 text-slate-500">
              {open ? "▾" : "▸"}
            </span>
          )}
          {lock.addr}
        </td>
        <td className="max-w-0 py-1 pr-3" title={top?.stack.replace(/;/g, " → ")}>
          <span className="flex items-baseline gap-2">
            <span className="truncate font-mono text-slate-100">
              {top ? blameFrame(top.stack) : <span className="text-slate-600">—</span>}
            </span>
            {lock.sites.length > 1 && (
              // Outside the truncation: "+2 more" is the shortest thing on the
              // row and the one that says the answer is not the whole answer.
              <span className="shrink-0 text-xs text-slate-500">
                +{lock.sites.length - 1} more
              </span>
            )}
          </span>
        </td>
        <td className="py-1 pr-3 text-right tabular-nums text-slate-200">
          {formatUs(lock.totalUs)}
        </td>
        <td className="py-1 pr-3 text-right tabular-nums">
          <ShareBar share={lock.share} />
        </td>
        <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
          {lock.calls.toLocaleString()}
        </td>
        <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
          {lock.avgUs !== null ? `${lock.avgUs.toFixed(0)} µs` : "—"}
        </td>
        <td className="py-1 text-right tabular-nums text-slate-400">
          {(perThread * 100).toFixed(1)}%
        </td>
      </tr>
      {open &&
        lock.sites.map((site) => (
          <tr key={site.stack} className="bg-slate-950/60 text-xs">
            <td />
            <td className="py-1 pr-3" colSpan={2}>
              <div className="font-mono text-slate-200">{blameFrame(site.stack)}</div>
              <div className="break-all text-slate-500">
                {site.stack.replace(/;/g, " → ")}
              </div>
            </td>
            <td className="py-1 pr-3 text-right tabular-nums text-slate-300">
              {formatUs(site.totalUs)}
            </td>
            <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
              {((site.totalUs / lock.totalUs) * 100).toFixed(0)}% of this lock
            </td>
            <td className="py-1 pr-3 text-right tabular-nums text-slate-500">
              {site.calls.toLocaleString()}
            </td>
            <td className="py-1 text-right tabular-nums text-slate-500">
              {site.avgUs !== null ? `${site.avgUs.toFixed(0)} µs` : "—"}
            </td>
          </tr>
        ))}
    </>
  );
}

function ShareBar({ share }: { share: number }) {
  return (
    <span className="flex items-center justify-end gap-2">
      <span className="h-1.5 w-20 overflow-hidden rounded bg-slate-800">
        <span
          className={`block h-full ${share >= 0.5 ? "bg-amber-400" : "bg-sky-500"}`}
          style={{ width: `${Math.max(1, share * 100)}%` }}
        />
      </span>
      <span className={share >= 0.5 ? "text-amber-300" : "text-slate-400"}>
        {(share * 100).toFixed(1)}%
      </span>
    </span>
  );
}

/**
 * These totals span six orders of magnitude — a few hundred microseconds for
 * an uncontended lock, hours of summed thread time for a bottleneck — so the
 * precision has to follow the magnitude. "7182.00 s" is two decimal places of
 * noise on a number whose leading digit is the only part anyone reads.
 */
function formatUs(us: number): string {
  const seconds = us / 1e6;
  if (seconds >= 100) return `${Math.round(seconds).toLocaleString()} s`;
  if (seconds >= 1) return `${seconds.toFixed(1)} s`;
  if (us >= 1e3) return `${(us / 1e3).toFixed(1)} ms`;
  return `${us.toFixed(0)} µs`;
}
