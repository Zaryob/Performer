/** One run, summarised: what was measured, and whether to believe it. */

import { useMemo } from "react";
import type { Bundle } from "../bundle/load";
import { runDuration } from "../bundle/load";
import { bundleQualityFlags, formatOverheadPct, usableOverheadPct } from "../quality";
import { verdict as computeVerdict, type Verdict } from "../analysis";
import { PmuSummary } from "../components/PmuSummary";
import {
  Field,
  FlagList,
  Panel,
  StatusBadge,
  formatBytes,
  formatDuration,
} from "../components/ui";

export function Overview({ bundle }: { bundle: Bundle }) {
  const manifest = bundle.manifest;
  const flags = bundleQualityFlags(bundle);
  const quality = manifest.quality;
  const overheadPct = usableOverheadPct(quality);
  const untracedCpuPct = Math.max(
    quality.overhead?.cpu_pct_before ?? -1,
    quality.overhead?.cpu_pct_after ?? -1,
  );
  const system = bundle.system;
  const verdict = useMemo(() => computeVerdict(bundle), [bundle]);

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="lg:col-span-2">
        <VerdictPanel verdict={verdict} />
      </div>

      <Panel
        title={manifest.run_id}
        right={<StatusBadge status={manifest.status} />}
      >
        <dl className="space-y-1.5">
          <Field label="label">{manifest.label}</Field>
          {(manifest.tags ?? []).length > 0 && (
            <Field label="tags">{(manifest.tags ?? []).join(", ")}</Field>
          )}
          <Field label="profile">{manifest.profile}</Field>
          <Field label="started">{manifest.started_at}</Field>
          <Field label="duration">
            {formatDuration(runDuration(manifest))}
            {manifest.actual_duration_s !== undefined &&
              manifest.actual_duration_s !== manifest.duration_s && (
                <span className="text-slate-400">
                  {" "}
                  (requested {formatDuration(manifest.duration_s)})
                </span>
              )}
          </Field>
          <Field label="target">
            pid {manifest.target.pid} · {manifest.target.comm}
          </Field>
          <Field label="threads">
            {manifest.target.thread_count_start} → {manifest.target.thread_count_end}
          </Field>
          <Field label="tooling">
            bpftrace {manifest.tool_versions.bpftrace ?? "n/a"} · kernel{" "}
            {manifest.tool_versions.kernel ?? "n/a"} · performer{" "}
            {manifest.tool_versions.performer ?? "?"}
          </Field>
          {manifest.notes && <Field label="notes">{manifest.notes}</Field>}
        </dl>
      </Panel>

      <Panel title="Quality">
        <dl className="mb-3 space-y-1.5">
          <Field label="frame pointers">
            {quality.frame_pointers_ok ? (
              <span className="text-emerald-300">ok</span>
            ) : (
              <span className="text-red-300">missing</span>
            )}
          </Field>
          <Field label="unknown frames">
            {(quality.unknown_frame_ratio * 100).toFixed(1)}%
            {quality.total_frame_samples ? (
              <span className="text-slate-400">
                {" "}
                of {quality.total_frame_samples.toLocaleString()} sampled frames
              </span>
            ) : null}
          </Field>
          <Field label="est. overhead">
            {formatOverheadPct(quality)}
            {overheadPct !== null && untracedCpuPct >= 0 && quality.overhead?.cpu_pct_during !== undefined && (
              <span className="text-slate-400">
                {" "}
                (target CPU {untracedCpuPct.toFixed(0)}% untraced
                → {quality.overhead.cpu_pct_during.toFixed(0)}% traced)
              </span>
            )}
          </Field>
        </dl>
        <FlagList flags={flags} />
      </Panel>

      <div className="lg:col-span-2"><PmuSummary bundle={bundle} /></div>

      <Panel title="Probes">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="py-1 pr-3">Probe</th>
              <th className="py-1 pr-3">Status</th>
              <th className="py-1 pr-3 text-right">Lost</th>
              <th className="py-1 pr-3">Thresholds</th>
              <th className="py-1">Notes</th>
            </tr>
          </thead>
          <tbody>
            {manifest.probes.map((probe) => (
              <tr key={probe.name} className="border-t border-slate-800 align-top">
                <td className="py-1.5 pr-3 font-mono text-slate-100">{probe.name}</td>
                <td className="py-1.5 pr-3">
                  <StatusBadge status={probe.status} />
                </td>
                <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                  {(probe.events_lost ?? 0).toLocaleString()}
                </td>
                <td className="py-1.5 pr-3 font-mono text-xs text-slate-400">
                  {probe.thresholds
                    ? Object.entries(probe.thresholds)
                        .map(([key, value]) => `${key}=${value}`)
                        .join(" ")
                    : "—"}
                </td>
                <td className="py-1.5 text-xs text-slate-400">
                  {(probe.warnings ?? []).join("; ") ||
                    (probe.exit_reason && probe.exit_reason !== "sigint"
                      ? probe.exit_reason
                      : "")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Host and artifacts">
        {system && (
          <dl className="mb-3 space-y-1.5">
            <Field label="kernel">
              {system.kernel} · {system.arch ?? "?"}
            </Field>
            <Field label="distro">{system.distro ?? "unknown"}</Field>
            <Field label="cpus">
              {system.cpu_count}
              {system.cpu_model ? ` · ${system.cpu_model}` : ""}
            </Field>
            {system.cgroup?.cpu_max && system.cgroup.cpu_max !== "max 100000" && (
              <Field label="cgroup cpu.max">{system.cgroup.cpu_max}</Field>
            )}
          </dl>
        )}
        <ul className="space-y-0.5 font-mono text-xs text-slate-400">
          {(manifest.files ?? [])
            .filter((file) => !file.path.startsWith("raw/"))
            .map((file) => (
              <li key={file.path} className="flex justify-between gap-4">
                <span className="text-slate-300">{file.path}</span>
                <span className="tabular-nums">
                  {formatBytes(file.bytes)}
                  {file.rows !== undefined && ` · ${file.rows.toLocaleString()} rows`}
                </span>
              </li>
            ))}
        </ul>
      </Panel>
    </div>
  );
}

/**
 * The verdict, at the top of the first screen anyone opens.
 *
 * A machine-written conclusion is worth having only if it is falsifiable on
 * sight, so the claim never appears without the numbers under it and the
 * screen that shows the working. "No single bottleneck" is presented exactly
 * as prominently as a finding, because on a real profile it is the more
 * common answer and burying it would push the reader toward inventing one.
 */
function VerdictPanel({ verdict }: { verdict: Verdict }) {
  const tone =
    verdict.kind === "untrustworthy"
      ? "border-red-500/50 bg-red-500/10"
      : verdict.kind === "none"
        ? "border-slate-600 bg-slate-800/40"
        : "border-amber-500/50 bg-amber-500/10";

  return (
    <section className={`rounded-lg border px-4 py-3 ${tone}`}>
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          Verdict
        </h2>
        <span className="text-xs text-slate-500">
          {verdict.confidence} confidence
        </span>
      </div>
      <p className="mt-1 text-base text-slate-100">{verdict.headline}</p>
      {verdict.evidence.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-sm text-slate-300">
          {verdict.evidence.map((line) => (
            <li key={line} className="flex gap-2">
              <span className="text-slate-500">·</span>
              <span>{line}</span>
            </li>
          ))}
        </ul>
      )}
      {verdict.next && (
        <p className="mt-2 text-sm text-slate-400">→ {verdict.next}</p>
      )}
    </section>
  );
}
