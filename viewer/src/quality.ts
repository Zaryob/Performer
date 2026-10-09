/**
 * Quality flags.
 *
 * These thresholds and messages mirror `collector/performer/report.py`. They
 * are duplicated because the two halves of the project share no runtime, and
 * duplication that drifts is worse than none -- so the numbers live at the top
 * of both files, and the collector's `inspect` output is the reference when
 * they need changing.
 */

import { collectionTiming } from "./collectionWindow";
import { readJson, runDuration, type Bundle } from "./bundle/load";
import { PATHS, type HistogramDoc, type Manifest, type Quality } from "./bundle/types";

export const THRESHOLDS = {
  /** Above this, flame graphs are actively misleading rather than merely poor. */
  unknownFrameRatioError: 0.3,
  unknownFrameRatioWarn: 0.1,
  overheadPctWarn: 15,
  overheadPctError: 30,
} as const;

export type FlagLevel = "error" | "warn" | "info";

export interface Flag {
  level: FlagLevel;
  code: string;
  message: string;
  hint?: string;
}

const ORDER: Record<FlagLevel, number> = { error: 0, warn: 1, info: 2 };

const percent = (value: number, digits = 0) => `${(value * 100).toFixed(digits)}%`;

/** Preserve valid zeroes while hiding unknown estimates in older bundles. */
export function usableOverheadPct(quality: Quality): number | null {
  const value = quality.estimated_overhead_pct;
  if (value === null) return null;
  // Earlier collectors wrote 0.0 even alongside a note that the estimate
  // could not be made or that the untraced baseline was unstable.
  if ((quality.notes ?? []).some((note) =>
    note.includes("overhead could not be estimated") ||
    note.includes("overhead estimate is unreliable"))) return null;
  return value;
}

export function formatOverheadPct(quality: Quality): string {
  const value = usableOverheadPct(quality);
  return value === null ? "n/a" : `${value.toFixed(1)}%`;
}

/** Inspect artifacts too: old runqlat probes could mix system-wide tasks. */
export function bundleQualityFlags(bundle: Bundle): Flag[] {
  const flags = qualityFlags(bundle.manifest);
  const timing = collectionTiming(bundle);
  if (timing.state === "uncertified" || timing.state === "invalid") {
    flags.unshift({ level: "warn", code: "capture_window", message: timing.state === "invalid"
      ? "capture timing evidence is invalid"
      : "a shared capture window could not be certified; compare probe totals with care" });
  }
  if (!bundle.files.has(PATHS.runqlat)) return flags;
  if (bundle.manifest.schema_version === 1) {
    flags.push({
      level: "error",
      code: "runqlat_unreliable",
      message: "legacy run queue histogram includes system-wide tasks; ignore this histogram",
      hint: "Recollect with the corrected runqlat probe; aggregated old data cannot be repaired.",
    });
    flags.sort((a, b) => ORDER[a.level] - ORDER[b.level]);
    return flags;
  }

  try {
    const histogram = readJson<HistogramDoc>(bundle, PATHS.runqlat);
    const duration = runDuration(bundle.manifest);
    const maxPlausibleUs = Math.max(duration * 2, duration + 30) * 1_000_000;
    if (!histogram || !Array.isArray(histogram.series)) throw new Error("invalid series");
    const impossible = histogram.series.some((series) => {
      if (!Array.isArray(series?.buckets)) throw new Error("invalid buckets");
      return series.buckets.some((bucket) => {
        if (typeof bucket?.count !== "number" ||
            (bucket.lo !== null && typeof bucket.lo !== "number")) {
          throw new Error("invalid bucket");
        }
        return bucket.count > 0 && bucket.lo !== null && bucket.lo >= maxPlausibleUs;
      });
    });
    if (impossible) {
      flags.push({
        level: "error",
        code: "runqlat_unreliable",
        message: "run queue histogram contains waits longer than the collection window; ignore this histogram",
        hint: "Recollect with the corrected runqlat probe; aggregated old data cannot be repaired.",
      });
    }
  } catch {
    flags.push({
      level: "error",
      code: "runqlat_unreadable",
      message: "run queue histogram could not be read",
    });
  }
  flags.sort((a, b) => ORDER[a.level] - ORDER[b.level]);
  return flags;
}

export function qualityFlags(manifest: Manifest): Flag[] {
  const flags: Flag[] = [];
  const quality = manifest.quality;

  if (quality.unknown_frame_ratio > THRESHOLDS.unknownFrameRatioError) {
    flags.push({
      level: "error",
      code: "unknown_frames",
      message: `${percent(
        quality.unknown_frame_ratio,
      )} of sampled frames are [unknown]; flame graphs are misleading`,
      hint: "Rebuild the target with -fno-omit-frame-pointer, or collect with DWARF unwinding.",
    });
  } else if (quality.unknown_frame_ratio > THRESHOLDS.unknownFrameRatioWarn) {
    flags.push({
      level: "warn",
      code: "unknown_frames",
      message: `${percent(quality.unknown_frame_ratio)} of sampled frames are [unknown]`,
      hint: "Deep leaf frames may be attributed to the wrong caller.",
    });
  }

  if (quality.frame_pointers_ok === false) {
    flags.push({
      level: "error",
      code: "frame_pointers",
      message: "stack trial found incomplete unwinding or symbol resolution",
      hint: "Check matching debug symbols and unwind information; consider frame pointers or DWARF.",
    });
  }
  if (quality.ignore_quality) {
    flags.push({
      level: "warn",
      code: "ignore_quality",
      message: "collected with --ignore-quality; the frame pointer check was overridden",
    });
  }

  const overhead = usableOverheadPct(quality);
  if (overhead !== null && overhead > THRESHOLDS.overheadPctError) {
    flags.push({
      level: "error",
      code: "overhead",
      message: `estimated overhead ${overhead.toFixed(
        1,
      )}% -- the measurement changed the workload`,
      hint: "Use a lighter profile or a shorter duration before drawing conclusions.",
    });
  } else if (overhead !== null && overhead > THRESHOLDS.overheadPctWarn) {
    flags.push({
      level: "warn",
      code: "overhead",
      message: `estimated overhead ${overhead.toFixed(1)}%`,
      hint: "Comparisons against a run with different overhead are unreliable.",
    });
  }

  if (manifest.status === "failed") {
    flags.push({
      level: "error",
      code: "run_failed",
      message: "no probe produced usable data",
    });
  } else if (manifest.status === "partial") {
    flags.push({
      level: "warn",
      code: "run_partial",
      message: "run is incomplete; see the probe table",
    });
  }

  if (manifest.target_died_at) {
    flags.push({
      level: "warn",
      code: "target_died",
      message: `target process exited during the run at ${manifest.target_died_at}`,
    });
  }

  for (const probe of manifest.probes ?? []) {
    if (probe.status === "failed") {
      flags.push({
        level: "error",
        code: "probe_failed",
        message: `probe '${probe.name}' produced nothing`,
      });
    } else if (probe.status === "partial") {
      flags.push({
        level: "warn",
        code: "probe_partial",
        message: `probe '${probe.name}' is partial`,
      });
    }
    if (probe.events_lost) {
      flags.push({
        level: "warn",
        code: "events_lost",
        message: `probe '${probe.name}' lost ${probe.events_lost.toLocaleString()} events; its totals are a lower bound`,
        hint: "Raise BPFTRACE_MAX_MAP_KEYS or narrow the probe's filter.",
      });
    }
    if (probe.exit_reason === "sigkill") {
      flags.push({
        level: "error",
        code: "probe_sigkill",
        message: `probe '${probe.name}' was SIGKILLed, so bpftrace never dumped its maps`,
      });
    }
  }

  for (const note of quality.notes ?? []) {
    flags.push({ level: "warn", code: "quality_note", message: note });
  }
  for (const warning of manifest.warnings ?? []) {
    flags.push({ level: "warn", code: "run_warning", message: warning });
  }

  flags.sort((a, b) => ORDER[a.level] - ORDER[b.level]);
  return flags;
}

export function worstLevel(flags: Flag[]): FlagLevel | null {
  if (!flags.length) return null;
  return flags.reduce<FlagLevel>(
    (worst, flag) => (ORDER[flag.level] < ORDER[worst] ? flag.level : worst),
    "info",
  );
}

/** True when the stacks in this run cannot carry an argument. */
export function stacksAreTrustworthy(manifest: Manifest): boolean {
  return (
    manifest.quality.frame_pointers_ok !== false &&
    manifest.quality.unknown_frame_ratio <= THRESHOLDS.unknownFrameRatioError
  );
}
