import type { Bundle } from "../bundle/load";
import { Field, Panel } from "./ui";
import { count, ratio, readPmu, runningShare, value } from "../pmu";

export function PmuSummary({ bundle }: { bundle: Bundle }) {
  const pmu = readPmu(bundle);
  if (!pmu) return null;
  const ipc = ratio(pmu.totals.instructions, pmu.totals.cycles);
  const branchMiss = ratio(pmu.totals.branch_misses, pmu.totals.branches);
  const cacheMiss = ratio(pmu.totals.cache_misses, pmu.totals.cache_references);
  const running = Object.values(pmu.totals)
    .map((event) => runningShare(event))
    .filter((share): share is number => share !== null);
  const leastRunning = running.length ? Math.min(...running) : null;

  return (
    <Panel title="Hardware counters · user space">
      <p className="mb-3 text-xs text-slate-400">
        Process totals across {pmu.threads_measured.toLocaleString()} measured threads
        {pmu.cpu_model ? ` · ${pmu.cpu_model}` : ""}. These counters are not attributed
        to individual functions.
      </p>
      <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
        <Field label="threads measured">{pmu.threads_measured.toLocaleString()} (started with {pmu.thread_count_start.toLocaleString()})</Field>
        <Field label="cycles">{count(value(pmu.totals.cycles))}</Field>
        <Field label="instructions">{count(value(pmu.totals.instructions))}</Field>
        <Field label="IPC">{ipc === null ? "—" : ipc.toFixed(2)}</Field>
        <Field label="branches">{count(value(pmu.totals.branches))}</Field>
        <Field label="branch misses">{count(value(pmu.totals.branch_misses))}</Field>
        <Field label="branch miss rate">{branchMiss === null ? "—" : `${(branchMiss * 100).toFixed(1)}%`}</Field>
        <Field label="cache references">{count(value(pmu.totals.cache_references))}</Field>
        <Field label="cache misses">{count(value(pmu.totals.cache_misses))}</Field>
        <Field label="cache miss rate">{cacheMiss === null ? "—" : `${(cacheMiss * 100).toFixed(1)}%`}</Field>
        <Field label="least counter time running">{leastRunning === null ? "—" : `${(leastRunning * 100).toFixed(1)}%`}</Field>
      </dl>
      {pmu.status !== "ok" && (
        <p className="mt-3 text-sm text-amber-200">PMU data is {pmu.status}; treat missing or scaled values with care.</p>
      )}
      {pmu.warnings.length > 0 && (
        <ul className="mt-2 list-inside list-disc text-xs text-amber-200">
          {pmu.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
    </Panel>
  );
}
