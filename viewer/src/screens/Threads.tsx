/**
 * The Threads table: 315 rows, sortable.
 *
 * Built from `meta/threads.json`, which comes from /proc rather than from a
 * probe. That matters: this is the one screen that still has something to say
 * when every eBPF probe failed.
 */

import { useMemo, useState } from "react";
import type { Bundle } from "../bundle/load";
import type { Schedstat } from "../bundle/types";
import { Empty, Panel } from "../components/ui";

interface Row {
  tid: number;
  name: string;
  cpuMs: number;
  waitMs: number;
  timeslices: number;
  voluntary: number | null;
  involuntary: number | null;
  lifecycle: string;
}

type SortKey = keyof Row;

const COLUMNS: { key: SortKey; label: string; numeric: boolean; title?: string }[] = [
  { key: "tid", label: "tid", numeric: true },
  { key: "name", label: "name", numeric: false },
  {
    key: "cpuMs",
    label: "CPU ms",
    numeric: true,
    title: "schedstat run time over the run",
  },
  {
    key: "waitMs",
    label: "runqueue ms",
    numeric: true,
    title: "time runnable but not running",
  },
  { key: "timeslices", label: "slices", numeric: true },
  { key: "voluntary", label: "vol ctxsw", numeric: true },
  { key: "involuntary", label: "invol ctxsw", numeric: true },
  { key: "lifecycle", label: "lifecycle", numeric: false },
];

function delta(
  start: Schedstat | null | undefined,
  end: Schedstat | null | undefined,
  field: keyof Schedstat,
): number {
  const from = (start?.[field] as number | undefined) ?? 0;
  const to = (end?.[field] as number | undefined) ?? 0;
  // A thread that appeared mid-run has no start sample; its end value is
  // already the whole of its life, so the difference is the right answer
  // either way.
  return Math.max(0, to - from);
}

export function Threads({ bundle }: { bundle: Bundle }) {
  const [sortKey, setSortKey] = useState<SortKey>("cpuMs");
  const [ascending, setAscending] = useState(false);
  const [filter, setFilter] = useState("");

  const rows = useMemo<Row[]>(() => {
    const doc = bundle.threads;
    if (!doc) return [];
    return Object.entries(doc.threads).map(([tid, entry]) => {
      const start = entry.start_schedstat;
      const end = entry.end_schedstat;
      return {
        tid: Number(tid),
        name: entry.name,
        cpuMs: delta(start, end, "run_ns") / 1e6,
        waitMs: delta(start, end, "wait_ns") / 1e6,
        timeslices: delta(start, end, "timeslices"),
        voluntary: end?.voluntary_ctxt_switches ?? null,
        involuntary: end?.nonvoluntary_ctxt_switches ?? null,
        lifecycle: entry.exited
          ? "exited"
          : entry.first_seen === "end"
            ? "started mid-run"
            : "present throughout",
      };
    });
  }, [bundle]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const filtered = needle
      ? rows.filter(
          (row) =>
            row.name.toLowerCase().includes(needle) || String(row.tid).includes(needle),
        )
      : rows;
    const sorted = [...filtered].sort((a, b) => {
      const left = a[sortKey];
      const right = b[sortKey];
      if (typeof left === "number" && typeof right === "number") {
        return ascending ? left - right : right - left;
      }
      const comparison = String(left ?? "").localeCompare(String(right ?? ""));
      return ascending ? comparison : -comparison;
    });
    return sorted;
  }, [rows, filter, sortKey, ascending]);

  if (!rows.length) {
    return <Empty>This bundle has no thread inventory (meta/threads.json).</Empty>;
  }

  const totals = visible.reduce(
    (acc, row) => ({ cpu: acc.cpu + row.cpuMs, wait: acc.wait + row.waitMs }),
    { cpu: 0, wait: 0 },
  );

  const toggle = (key: SortKey) => {
    if (key === sortKey) setAscending((value) => !value);
    else {
      setSortKey(key);
      setAscending(false);
    }
  };

  return (
    <Panel
      title={`Threads (${visible.length.toLocaleString()} of ${rows.length.toLocaleString()})`}
      right={
        <input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="filter by name or tid"
          className="w-56 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
        />
      }
    >
      <p className="mb-2 text-xs text-slate-500">
        From <code>/proc</code>, so these numbers survive even when every probe
        failed. CPU and runqueue times are the difference between the snapshots
        taken at each end of the run.
      </p>
      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  title={column.title}
                  onClick={() => toggle(column.key)}
                  className={`cursor-pointer py-1 pr-3 select-none hover:text-slate-200 ${
                    column.numeric ? "text-right" : ""
                  }`}
                >
                  {column.label}
                  {sortKey === column.key && (ascending ? " ▲" : " ▼")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <tr key={row.tid} className="border-t border-slate-800">
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.tid}
                </td>
                <td className="py-1 pr-3 font-mono text-slate-100">{row.name}</td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-200">
                  {row.cpuMs.toFixed(1)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-300">
                  {row.waitMs.toFixed(1)}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.timeslices.toLocaleString()}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.voluntary?.toLocaleString() ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.involuntary?.toLocaleString() ?? "—"}
                </td>
                <td className="py-1 text-slate-400">{row.lifecycle}</td>
              </tr>
            ))}
          </tbody>
          <tfoot className="sticky bottom-0 bg-slate-900 text-xs text-slate-400">
            <tr className="border-t border-slate-700">
              <td className="py-1 pr-3" colSpan={2}>
                total shown
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {totals.cpu.toFixed(0)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {totals.wait.toFixed(0)}
              </td>
              <td colSpan={4} />
            </tr>
          </tfoot>
        </table>
      </div>
    </Panel>
  );
}
