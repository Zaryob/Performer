/**
 * The Threads table: 315 rows, sortable.
 *
 * Built from `meta/threads.json`, which comes from /proc rather than from a
 * probe. That matters: this is the one screen that still has something to say
 * when every eBPF probe failed.
 *
 * With a baseline run selected it also compares -- but by thread *name*, not
 * by tid. Tids are not stable across runs: restart the process and the same
 * worker is a different number, so a per-tid diff would report 315 threads
 * destroyed and 315 created every time. Names are what a thread pool actually
 * has in common between runs, and grouping by them answers the question that
 * matters here anyway: did this pool grow, and did its threads spend longer
 * waiting for the CPU.
 */

import { useMemo, useState } from "react";
import type { Bundle } from "../bundle/load";
import { runDuration } from "../bundle/load";
import type { Schedstat, ThreadsDoc } from "../bundle/types";
import { Empty, Panel } from "../components/ui";
import { ratio, readPmu, value } from "../pmu";

interface Row {
  key: string;
  tid: number;
  name: string;
  cpuMs: number | null;
  waitMs: number | null;
  timeslices: number | null;
  voluntary: number | null;
  involuntary: number | null;
  lifecycle: string;
  pmuCycles: number | null;
  pmuIpc: number | null;
  pmuCoverage: number | null;
}

type SortKey = Exclude<keyof Row, "key">;

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
  { key: "pmuCycles", label: "PMU cycles", numeric: true, title: "user-space cycles measured for this thread" },
  { key: "pmuIpc", label: "IPC", numeric: true, title: "instructions per cycle; hidden if counter scheduling was poor" },
  { key: "pmuCoverage", label: "PMU s", numeric: true, title: "seconds this thread had PMU counters attached" },
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

export function Threads({
  bundle,
  baseline = null,
}: {
  bundle: Bundle;
  baseline?: Bundle | null;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("cpuMs");
  const [ascending, setAscending] = useState(false);
  const [filter, setFilter] = useState("");
  const hasPmu = readPmu(bundle) !== null;
  const columns = hasPmu ? COLUMNS : COLUMNS.filter((column) => !column.key.startsWith("pmu"));

  const rows = useMemo<Row[]>(() => {
    const doc = bundle.threads;
    const measured = readPmu(bundle)?.threads ?? [];
    const pmuThreads = new Map<number, (NonNullable<ReturnType<typeof readPmu>>)["threads"]>();
    for (const thread of measured) {
      const matching = pmuThreads.get(thread.tid) ?? [];
      matching.push(thread);
      pmuThreads.set(thread.tid, matching);
    }
    const matched = new Set<string>();
    const rows: Row[] = Object.entries(doc?.threads ?? {}).map(([tid, entry]) => {
      const start = entry.start_schedstat;
      const end = entry.end_schedstat;
      const candidates = pmuThreads.get(Number(tid)) ?? [];
      const onlyCandidate = candidates.length === 1 ? candidates[0] : undefined;
      const pmu = onlyCandidate?.start_time_ticks === entry.start_time_ticks ? onlyCandidate : null;
      if (pmu) matched.add(`${pmu.tid}:${pmu.start_time_ticks}`);
      return {
        key: tid,
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
        pmuCycles: pmu ? value(pmu.events.cycles) : null,
        pmuIpc: pmu ? ratio(pmu.events.instructions, pmu.events.cycles) : null,
        pmuCoverage: pmu?.coverage_s ?? null,
      };
    });
    for (const thread of measured) {
      const key = `${thread.tid}:${thread.start_time_ticks}`;
      if (matched.has(key)) continue;
      rows.push({
        key,
        tid: thread.tid,
        name: thread.name,
        cpuMs: null,
        waitMs: null,
        timeslices: null,
        voluntary: null,
        involuntary: null,
        lifecycle: "PMU only",
        pmuCycles: value(thread.events.cycles),
        pmuIpc: ratio(thread.events.instructions, thread.events.cycles),
        pmuCoverage: thread.coverage_s,
      });
    }
    return rows;
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
    return <Empty>This bundle has no thread inventory or PMU thread counts.</Empty>;
  }

  const totals = visible.reduce(
    (acc, row) => ({ cpu: acc.cpu + (row.cpuMs ?? 0), wait: acc.wait + (row.waitMs ?? 0) }),
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
    <div className="space-y-4">
      {baseline && <ThreadDelta a={baseline} b={bundle} />}
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
        taken at each end of the run. PMU-only rows were measured between those
        snapshots or could not be safely matched by thread identity.
      </p>
      <div className="max-h-[32rem] overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              {columns.map((column) => (
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
              <tr key={row.key} className="border-t border-slate-800">
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.tid}
                </td>
                <td className="py-1 pr-3 font-mono text-slate-100">{row.name}</td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-200">
                  {row.cpuMs?.toFixed(1) ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-300">
                  {row.waitMs?.toFixed(1) ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.timeslices?.toLocaleString() ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.voluntary?.toLocaleString() ?? "—"}
                </td>
                <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                  {row.involuntary?.toLocaleString() ?? "—"}
                </td>
                <td className="py-1 text-slate-400">{row.lifecycle}</td>
                {hasPmu && <>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-300">{row.pmuCycles?.toLocaleString() ?? "—"}</td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-300">{row.pmuIpc?.toFixed(2) ?? "—"}</td>
                  <td className="py-1 pr-3 text-right tabular-nums text-slate-300">{row.pmuCoverage?.toFixed(1) ?? "—"}</td>
                </>}
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
              <td colSpan={hasPmu ? 7 : 4} />
            </tr>
          </tfoot>
        </table>
      </div>
      </Panel>
    </div>
  );
}

// -- comparison against a baseline run --------------------------------------

interface NameGroup {
  name: string;
  count: number;
  cpuMs: number;
  waitMs: number;
}

/**
 * Sum every thread with the same name into one row, and divide by the run's
 * length.
 *
 * Both halves matter. Summing is what makes the two runs joinable at all,
 * since a 200 thread pool and a 315 thread pool have no tids in common and no
 * one-to-one correspondence to find. Dividing by the duration is what stops a
 * 90 s run looking like a regression against a 60 s one -- the same mistake
 * normalisation prevents on the Diff screen, in the same place.
 */
function groupByName(doc: ThreadsDoc | null, seconds: number): Map<string, NameGroup> {
  const groups = new Map<string, NameGroup>();
  if (!doc) return groups;
  const scale = seconds > 0 ? 1 / seconds : 0;
  for (const entry of Object.values(doc.threads)) {
    const group = groups.get(entry.name) ?? {
      name: entry.name,
      count: 0,
      cpuMs: 0,
      waitMs: 0,
    };
    group.count += 1;
    group.cpuMs += (delta(entry.start_schedstat, entry.end_schedstat, "run_ns") / 1e6) * scale;
    group.waitMs +=
      (delta(entry.start_schedstat, entry.end_schedstat, "wait_ns") / 1e6) * scale;
    groups.set(entry.name, group);
  }
  return groups;
}

interface DeltaRow {
  name: string;
  countA: number;
  countB: number;
  cpuA: number;
  cpuB: number;
  waitA: number;
  waitB: number;
}

function signed(value: number, digits = 1): string {
  if (Math.abs(value) < 0.05 / 10 ** (digits - 1)) return "—";
  return `${value > 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

function ThreadDelta({ a, b }: { a: Bundle; b: Bundle }) {
  const [sortBy, setSortBy] = useState<"wait" | "cpu" | "count">("wait");

  const rows = useMemo<DeltaRow[]>(() => {
    const left = groupByName(a.threads, runDuration(a.manifest));
    const right = groupByName(b.threads, runDuration(b.manifest));
    const names = new Set([...left.keys(), ...right.keys()]);
    return [...names].map((name) => {
      const x = left.get(name);
      const y = right.get(name);
      return {
        name,
        countA: x?.count ?? 0,
        countB: y?.count ?? 0,
        cpuA: x?.cpuMs ?? 0,
        cpuB: y?.cpuMs ?? 0,
        waitA: x?.waitMs ?? 0,
        waitB: y?.waitMs ?? 0,
      };
    });
  }, [a, b]);

  const sorted = useMemo(() => {
    const key = (row: DeltaRow) =>
      sortBy === "cpu"
        ? Math.abs(row.cpuB - row.cpuA)
        : sortBy === "count"
          ? Math.abs(row.countB - row.countA)
          : Math.abs(row.waitB - row.waitA);
    return [...rows].sort((x, y) => key(y) - key(x) || x.name.localeCompare(y.name));
  }, [rows, sortBy]);

  if (!rows.length) {
    return (
      <Panel title="Versus baseline">
        <Empty>Neither run has a thread inventory to compare.</Empty>
      </Panel>
    );
  }

  const totals = rows.reduce(
    (acc, row) => ({
      countA: acc.countA + row.countA,
      countB: acc.countB + row.countB,
      cpuA: acc.cpuA + row.cpuA,
      cpuB: acc.cpuB + row.cpuB,
      waitA: acc.waitA + row.waitA,
      waitB: acc.waitB + row.waitB,
    }),
    { countA: 0, countB: 0, cpuA: 0, cpuB: 0, waitA: 0, waitB: 0 },
  );

  return (
    <Panel
      title={`Versus ${a.manifest.label}`}
      right={
        <label className="flex items-center gap-2 text-xs text-slate-400">
          sort by
          <select
            value={sortBy}
            onChange={(event) =>
              setSortBy(event.target.value as "wait" | "cpu" | "count")
            }
            className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
          >
            <option value="wait">runqueue change</option>
            <option value="cpu">CPU change</option>
            <option value="count">thread count change</option>
          </select>
        </label>
      }
    >
      <p className="mb-2 text-xs text-slate-500">
        Grouped by thread name, because tids are not stable across runs — a
        restarted process has none of the same ones. Times are per second of
        run, so the {a.manifest.duration_s.toFixed(0)} s and{" "}
        {b.manifest.duration_s.toFixed(0)} s runs are on the same scale.
      </p>
      <div className="max-h-96 overflow-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="py-1 pr-3">name</th>
              <th className="py-1 pr-3 text-right" colSpan={2} title="threads with this name">
                threads
              </th>
              <th className="py-1 pr-3 text-right" colSpan={2} title="CPU ms per second of run">
                CPU ms/s
              </th>
              <th className="py-1 text-right" colSpan={2} title="runqueue ms per second of run">
                runq ms/s
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.name} className="border-t border-slate-800">
                <td className="py-1 pr-3 font-mono text-slate-100">{row.name}</td>
                <td className="py-1 pr-1 text-right tabular-nums text-slate-400">
                  {row.countA} → {row.countB}
                </td>
                <td
                  className={`py-1 pr-3 text-right tabular-nums ${
                    row.countB > row.countA
                      ? "text-rose-300"
                      : row.countB < row.countA
                        ? "text-sky-300"
                        : "text-slate-600"
                  }`}
                >
                  {row.countB === row.countA ? "—" : signed(row.countB - row.countA, 0)}
                </td>
                <td className="py-1 pr-1 text-right tabular-nums text-slate-400">
                  {row.cpuA.toFixed(1)} → {row.cpuB.toFixed(1)}
                </td>
                <td
                  className={`py-1 pr-3 text-right tabular-nums ${
                    row.cpuB > row.cpuA ? "text-rose-300" : "text-sky-300"
                  }`}
                >
                  {signed(row.cpuB - row.cpuA)}
                </td>
                <td className="py-1 pr-1 text-right tabular-nums text-slate-400">
                  {row.waitA.toFixed(1)} → {row.waitB.toFixed(1)}
                </td>
                <td
                  className={`py-1 text-right tabular-nums ${
                    row.waitB > row.waitA ? "text-rose-300" : "text-sky-300"
                  }`}
                >
                  {signed(row.waitB - row.waitA)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot className="sticky bottom-0 bg-slate-900 text-xs text-slate-300">
            <tr className="border-t border-slate-700">
              <td className="py-1 pr-3">all threads</td>
              <td className="py-1 pr-1 text-right tabular-nums">
                {totals.countA} → {totals.countB}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {signed(totals.countB - totals.countA, 0)}
              </td>
              <td className="py-1 pr-1 text-right tabular-nums">
                {totals.cpuA.toFixed(1)} → {totals.cpuB.toFixed(1)}
              </td>
              <td className="py-1 pr-3 text-right tabular-nums">
                {signed(totals.cpuB - totals.cpuA)}
              </td>
              <td className="py-1 pr-1 text-right tabular-nums">
                {totals.waitA.toFixed(1)} → {totals.waitB.toFixed(1)}
              </td>
              <td className="py-1 text-right tabular-nums">
                {signed(totals.waitB - totals.waitA)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </Panel>
  );
}
