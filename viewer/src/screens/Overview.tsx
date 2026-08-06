/** One run, summarised: what was measured, and whether to believe it. */

import type { Bundle } from "../bundle/load";
import { runDuration } from "../bundle/load";
import { qualityFlags } from "../quality";
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
  const flags = qualityFlags(manifest);
  const quality = manifest.quality;
  const system = bundle.system;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
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
            {quality.estimated_overhead_pct.toFixed(1)}%
            {quality.overhead?.cpu_pct_during !== undefined && (
              <span className="text-slate-400">
                {" "}
                (target CPU {quality.overhead.cpu_pct_before?.toFixed(0) ?? "?"}% untraced
                → {quality.overhead.cpu_pct_during.toFixed(0)}% traced)
              </span>
            )}
          </Field>
        </dl>
        <FlagList flags={flags} />
      </Panel>

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
