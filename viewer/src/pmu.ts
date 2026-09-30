import type { Bundle } from "./bundle/load";
import { readJson } from "./bundle/load";
import { PATHS, type PmuDoc, type PmuEvent } from "./bundle/types";

export function readPmu(bundle: Bundle): PmuDoc | null {
  const doc = readJson<PmuDoc>(bundle, PATHS.pmu);
  if (!doc || doc.kind !== "pmu" || doc.mode !== "basic" ||
      !doc.totals || !Array.isArray(doc.threads)) return null;
  return doc;
}

export function value(event: PmuEvent | undefined): number | null {
  if (!event || event.scaled === null || !Number.isFinite(event.scaled)) return null;
  return event.scaled;
}

export function runningShare(event: PmuEvent | undefined): number | null {
  if (!event || event.time_enabled_ns <= 0) return null;
  return event.time_running_ns / event.time_enabled_ns;
}

export function ratio(
  numerator: PmuEvent | undefined,
  denominator: PmuEvent | undefined,
): number | null {
  const a = value(numerator);
  const b = value(denominator);
  if (a === null || b === null || b <= 0) return null;
  if ((runningShare(numerator) ?? 0) < 0.9 ||
      (runningShare(denominator) ?? 0) < 0.9) return null;
  return a / b;
}

export function count(value: number | null): string {
  return value === null ? "—" : Math.round(value).toLocaleString();
}
