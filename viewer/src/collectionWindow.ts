/** Optional timing evidence; older bundles remain readable without it. */
import { readJson, type Bundle } from "./bundle/load";

export const WINDOW_PATH = "meta/window.json";
export interface CollectionTiming {
  state: "missing" | "invalid" | "certified" | "uncertified";
  durationS?: number;
  clock?: string;
  pmuEnableMs?: [number, number];
  pmuDisableMs?: [number, number];
}
const timestamp = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && Number.isInteger(value);

export function collectionTiming(bundle: Bundle): CollectionTiming {
  if (!bundle.files.has(WINDOW_PATH)) return { state: "missing" };
  try {
    const doc = readJson<Record<string, unknown>>(bundle, WINDOW_PATH);
    if (!doc || !timestamp(doc.start_ns) || !timestamp(doc.end_ns) || doc.end_ns < doc.start_ns ||
        typeof doc.certified !== "boolean" || doc.clock !== "boottime") return { state: "invalid" };
    const result: CollectionTiming = {
      state: doc.certified ? "certified" : "uncertified",
      durationS: (doc.end_ns - doc.start_ns) / 1e9,
      clock: doc.clock,
    };
    const pmu = doc.pmu;
    if (pmu && typeof pmu === "object") {
      const p = pmu as Record<string, unknown>;
      if (timestamp(p.enable_begin_ns) && timestamp(p.enable_end_ns) && p.enable_end_ns >= p.enable_begin_ns)
        result.pmuEnableMs = [(p.enable_begin_ns - doc.start_ns) / 1e6, (p.enable_end_ns - doc.start_ns) / 1e6];
      if (timestamp(p.disable_begin_ns) && timestamp(p.disable_end_ns) && p.disable_end_ns >= p.disable_begin_ns)
        result.pmuDisableMs = [(p.disable_begin_ns - doc.end_ns) / 1e6, (p.disable_end_ns - doc.end_ns) / 1e6];
    }
    return result;
  } catch {
    return { state: "invalid" };
  }
}
