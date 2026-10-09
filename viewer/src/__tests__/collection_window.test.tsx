import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { type Bundle } from "../bundle/load";
import { collectionTiming, WINDOW_PATH } from "../collectionWindow";
import { bundleQualityFlags } from "../quality";
import { Overview } from "../screens/Overview";

function bundle(document?: unknown): Bundle {
  const result: Bundle = {
    key: "test", fileName: "test.tgz", loadedAt: 0, system: null, threads: null, files: new Map(),
    manifest: {
      schema_version: 2, run_id: "test", label: "test", started_at: "2026-10-10T00:00:00Z",
      duration_s: 20, profile: "standard", status: "ok", tool_versions: {}, probes: [],
      target: { pid: 1, comm: "app", thread_count_start: 9, thread_count_end: 9 },
      quality: { frame_pointers_ok: true, unknown_frame_ratio: 0, estimated_overhead_pct: null },
    },
  };
  if (document !== undefined) result.files.set(WINDOW_PATH, new TextEncoder().encode(JSON.stringify(document)));
  return result;
}
const timing = { schema_version: 2, clock: "boottime", start_ns: 1e9, end_ns: 21e9, certified: true };

describe("collection timing evidence", () => {
  it("keeps older bundles readable without inventing certification", () => {
    const old = bundle();
    expect(collectionTiming(old).state).toBe("missing");
    expect(renderToStaticMarkup(<Overview bundle={old} />)).toContain("timing evidence unavailable");
  });
  it("shows the gated duration and the actual batch offsets of PMU counters", () => {
    const run = bundle({ ...timing, pmu: { enable_begin_ns: 1e9 - 20e6, enable_end_ns: 1e9 + 10e6,
      disable_begin_ns: 21e9 + 30e6, disable_end_ns: 21e9 + 45e6 } });
    expect(collectionTiming(run)).toMatchObject({ state: "certified", durationS: 20,
      pmuEnableMs: [-20, 10], pmuDisableMs: [30, 45] });
    const html = renderToStaticMarkup(<Overview bundle={run} />);
    expect(html).toContain("20.000 s · boottime · certified");
    expect(html).toContain("per-thread counters are enabled in a batch");
  });
  it("warns on failed acknowledgement even when the manifest says ok", () => {
    const run = bundle({ ...timing, certified: false });
    expect(bundleQualityFlags(run).some((flag) => flag.code === "capture_window")).toBe(true);
    expect(renderToStaticMarkup(<Overview bundle={run} />)).toContain("uncertified");
  });
  it("does not turn corrupt or inconsistent evidence into a duration", () => {
    for (const document of [{ ...timing, end_ns: 0 }, { ...timing, certified: "true" },
      { ...timing, clock: "realtime" }, null]) {
      expect(collectionTiming(bundle(document)).state).toBe("invalid");
    }
    const run = bundle();
    run.files.set(WINDOW_PATH, new TextEncoder().encode("not json"));
    expect(collectionTiming(run).state).toBe("invalid");
    expect(bundleQualityFlags(run).some((flag) => flag.code === "capture_window")).toBe(true);
  });
  it("describes failed stack quality without asserting missing frame pointers", () => {
    const run = bundle(timing);
    run.manifest.quality.frame_pointers_ok = false;
    const html = renderToStaticMarkup(<Overview bundle={run} />);
    expect(html).toContain("stack quality");
    expect(html).toContain("insufficient");
    expect(html).not.toContain("frame pointers are missing");
  });
});
