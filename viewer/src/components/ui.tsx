/** Small shared pieces, kept together so the screens stay about their content. */

import type { ReactNode } from "react";
import type { Flag, FlagLevel } from "../quality";

export function Panel({
  title,
  right,
  children,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-900/60">
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-700 px-4 py-2">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 text-sm">
      <dt className="w-40 shrink-0 text-slate-400">{label}</dt>
      <dd className="min-w-0 break-words text-slate-100">{children}</dd>
    </div>
  );
}

const LEVEL_STYLE: Record<FlagLevel, string> = {
  error: "border-red-500/50 bg-red-500/10 text-red-200",
  warn: "border-amber-500/50 bg-amber-500/10 text-amber-100",
  info: "border-sky-500/50 bg-sky-500/10 text-sky-100",
};

const LEVEL_MARK: Record<FlagLevel, string> = {
  error: "✕",
  warn: "!",
  info: "i",
};

export function FlagList({ flags }: { flags: Flag[] }) {
  if (!flags.length) {
    return (
      <p className="text-sm text-emerald-300">
        No flags — this run looks trustworthy.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {flags.map((flag, index) => (
        <li
          key={`${flag.code}-${index}`}
          className={`rounded border px-3 py-2 text-sm ${LEVEL_STYLE[flag.level]}`}
        >
          <span className="mr-2 font-bold">{LEVEL_MARK[flag.level]}</span>
          {flag.message}
          {flag.hint && (
            <div className="mt-1 pl-5 text-xs opacity-80">→ {flag.hint}</div>
          )}
        </li>
      ))}
    </ul>
  );
}

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  partial: "bg-amber-500/15 text-amber-200 border-amber-500/40",
  failed: "bg-red-500/15 text-red-200 border-red-500/40",
  skipped: "bg-slate-600/20 text-slate-400 border-slate-500/40",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-xs font-medium ${
        STATUS_STYLE[status] ?? STATUS_STYLE.skipped
      }`}
    >
      {status}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded border border-dashed border-slate-700 px-4 py-6 text-center text-sm text-slate-400">
      {children}
    </p>
  );
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes / 1024;
  for (const unit of units) {
    if (value < 1024 || unit === "GiB") return `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
  return `${value.toFixed(1)} GiB`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
}
