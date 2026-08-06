/** The list of loaded bundles, and the only way anything gets into the viewer. */

import { useRef, useState } from "react";
import type { Bundle } from "../bundle/load";
import { loadBundleFile, BundleError, runDuration } from "../bundle/load";
import { qualityFlags, worstLevel } from "../quality";
import { Empty, Panel, StatusBadge, formatDuration } from "../components/ui";

export interface RunsProps {
  bundles: Bundle[];
  selected: string | null;
  onSelect: (key: string) => void;
  onAdd: (bundles: Bundle[]) => void;
  onRemove: (key: string) => void;
}

export function Runs({ bundles, selected, onSelect, onAdd, onRemove }: RunsProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);

  const ingest = async (files: FileList | File[]) => {
    setBusy(true);
    const loaded: Bundle[] = [];
    const failures: string[] = [];
    for (const file of Array.from(files)) {
      try {
        loaded.push(await loadBundleFile(file));
      } catch (error) {
        failures.push(
          error instanceof BundleError
            ? `${file.name}: ${error.message}`
            : `${file.name}: ${(error as Error).message}`,
        );
      }
    }
    setErrors(failures);
    if (loaded.length) onAdd(loaded);
    setBusy(false);
  };

  return (
    <div className="space-y-4">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (event.dataTransfer.files.length) void ingest(event.dataTransfer.files);
        }}
        className={`rounded-lg border-2 border-dashed px-6 py-8 text-center transition ${
          dragging
            ? "border-sky-400 bg-sky-500/10"
            : "border-slate-700 bg-slate-900/40"
        }`}
      >
        <p className="text-sm text-slate-300">
          Drop run bundles here, or{" "}
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="rounded bg-sky-600 px-2 py-1 text-white hover:bg-sky-500"
          >
            choose files
          </button>
        </p>
        <p className="mt-2 text-xs text-slate-500">
          Everything is read in this browser. Nothing is uploaded — the viewer has
          no server to upload to.
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".tgz,.tar.gz,.tar"
          className="hidden"
          onChange={(event) => {
            if (event.target.files?.length) void ingest(event.target.files);
            event.target.value = "";
          }}
        />
        {busy && <p className="mt-2 text-xs text-sky-300">reading…</p>}
      </div>

      {errors.length > 0 && (
        <ul className="space-y-1">
          {errors.map((message) => (
            <li
              key={message}
              className="rounded border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200"
            >
              {message}
            </li>
          ))}
        </ul>
      )}

      <Panel title={`Runs (${bundles.length})`}>
        {bundles.length === 0 ? (
          <Empty>
            No bundles loaded. Produce one with{" "}
            <code className="text-slate-300">performer collect</code>, or try{" "}
            <code className="text-slate-300">performer fake-run</code> without a
            target.
          </Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="py-1 pr-3">Label</th>
                  <th className="py-1 pr-3">Profile</th>
                  <th className="py-1 pr-3">Started</th>
                  <th className="py-1 pr-3 text-right">Duration</th>
                  <th className="py-1 pr-3 text-right">Threads</th>
                  <th className="py-1 pr-3 text-right">Overhead</th>
                  <th className="py-1 pr-3">Status</th>
                  <th className="py-1 pr-3">Flags</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {bundles.map((bundle) => {
                  const manifest = bundle.manifest;
                  const flags = qualityFlags(manifest);
                  const worst = worstLevel(flags);
                  const isSelected = bundle.key === selected;
                  return (
                    <tr
                      key={bundle.key}
                      onClick={() => onSelect(bundle.key)}
                      className={`cursor-pointer border-t border-slate-800 ${
                        isSelected ? "bg-sky-500/10" : "hover:bg-slate-800/50"
                      }`}
                    >
                      <td className="py-1.5 pr-3">
                        <div className="font-medium text-slate-100">
                          {manifest.label}
                        </div>
                        <div className="text-xs text-slate-500">
                          {(manifest.tags ?? []).join(", ") || bundle.fileName}
                        </div>
                      </td>
                      <td className="py-1.5 pr-3 text-slate-300">{manifest.profile}</td>
                      <td className="py-1.5 pr-3 text-slate-400">
                        {manifest.started_at.replace("T", " ").replace("Z", "")}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                        {formatDuration(runDuration(manifest))}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                        {manifest.target.thread_count_start}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-slate-300">
                        {manifest.quality.estimated_overhead_pct.toFixed(1)}%
                      </td>
                      <td className="py-1.5 pr-3">
                        <StatusBadge status={manifest.status} />
                      </td>
                      <td className="py-1.5 pr-3">
                        {worst === null ? (
                          <span className="text-emerald-400">clean</span>
                        ) : (
                          <span
                            className={
                              worst === "error" ? "text-red-300" : "text-amber-300"
                            }
                          >
                            {flags.length} {worst}
                            {flags.length > 1 ? "s" : ""}
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 text-right">
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            onRemove(bundle.key);
                          }}
                          className="text-xs text-slate-500 hover:text-red-300"
                        >
                          remove
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
