import { describe, expect, it } from "vitest";
import { bundleQualityFlags, formatOverheadPct, qualityFlags, stacksAreTrustworthy, usableOverheadPct, worstLevel, THRESHOLDS } from "../quality";
import type { Bundle } from "../bundle/load";
import { PATHS, type Manifest } from "../bundle/types";

function manifest(overrides: Partial<Manifest> = {}): Manifest {
  return {
    schema_version: 1,
    run_id: "20260806T142530Z-unit",
    label: "unit",
    started_at: "2026-08-06T14:25:30Z",
    duration_s: 60,
    profile: "standard",
    status: "ok",
    target: { pid: 1, comm: "app", thread_count_start: 315, thread_count_end: 315 },
    probes: [{ name: "oncpu", status: "ok" }],
    tool_versions: { performer: "0.1.0" },
    quality: {
      frame_pointers_ok: true,
      unknown_frame_ratio: 0,
      estimated_overhead_pct: 0,
    },
    ...overrides,
  };
}

describe("qualityFlags", () => {
  it("says nothing about a clean run", () => {
    expect(qualityFlags(manifest())).toEqual([]);
  });

  it("red flags unusable stacks, with the fix", () => {
    const flags = qualityFlags(
      manifest({
        quality: {
          frame_pointers_ok: false,
          unknown_frame_ratio: 0.58,
          estimated_overhead_pct: 0,
        },
      }),
    );
    const unknown = flags.find((flag) => flag.code === "unknown_frames")!;
    expect(unknown.level).toBe("error");
    expect(unknown.message).toContain("58%");
    expect(unknown.hint).toContain("-fno-omit-frame-pointer");
  });

  it("warns rather than errors just below the line", () => {
    const flags = qualityFlags(
      manifest({
        quality: {
          frame_pointers_ok: true,
          unknown_frame_ratio: THRESHOLDS.unknownFrameRatioError - 0.01,
          estimated_overhead_pct: 0,
        },
      }),
    );
    expect(flags.find((f) => f.code === "unknown_frames")!.level).toBe("warn");
  });

  it("flags overhead that changed the workload", () => {
    const flags = qualityFlags(
      manifest({
        quality: {
          frame_pointers_ok: true,
          unknown_frame_ratio: 0,
          estimated_overhead_pct: 42,
        },
      }),
    );
    expect(flags.find((f) => f.code === "overhead")!.level).toBe("error");
  });

  it("reports lost events as a lower bound", () => {
    const flags = qualityFlags(
      manifest({
        status: "partial",
        probes: [{ name: "futex", status: "partial", events_lost: 12043 }],
      }),
    );
    const lost = flags.find((f) => f.code === "events_lost")!;
    expect(lost.message).toContain("12,043");
    expect(lost.message).toContain("lower bound");
  });

  it("calls out a SIGKILLed probe, whose maps were never written", () => {
    const flags = qualityFlags(
      manifest({
        status: "partial",
        probes: [{ name: "offcpu", status: "failed", exit_reason: "sigkill" }],
      }),
    );
    expect(flags.find((f) => f.code === "probe_sigkill")!.level).toBe("error");
  });

  it("surfaces a target that died mid-run", () => {
    const flags = qualityFlags(
      manifest({ status: "partial", target_died_at: "2026-08-06T14:26:01Z" }),
    );
    expect(flags.some((f) => f.code === "target_died")).toBe(true);
  });

  it("carries the collector's own quality notes through", () => {
    const flags = qualityFlags(
      manifest({
        quality: {
          frame_pointers_ok: true,
          unknown_frame_ratio: 0,
          estimated_overhead_pct: 0,
          notes: ["the target's untraced CPU differed by 57% between samples"],
        },
      }),
    );
    expect(flags.some((f) => f.message.includes("57%"))).toBe(true);
  });

  it("puts errors before warnings", () => {
    const flags = qualityFlags(
      manifest({
        status: "partial",
        quality: {
          frame_pointers_ok: false,
          unknown_frame_ratio: 0.6,
          estimated_overhead_pct: 20,
        },
      }),
    );
    expect(flags[0]!.level).toBe("error");
    expect(worstLevel(flags)).toBe("error");
  });
});

describe("overhead estimate", () => {
  it("shows an unavailable estimate as n/a while retaining its warning", () => {
    const run = manifest({
      quality: {
        frame_pointers_ok: true,
        unknown_frame_ratio: 0,
        estimated_overhead_pct: null,
        notes: ["overhead could not be estimated: CPU baseline or samples were unavailable"],
      },
    });
    expect(formatOverheadPct(run.quality)).toBe("n/a");
    expect(qualityFlags(run).some((flag) => flag.code === "quality_note")).toBe(true);
  });

  it("hides the false zero in older unavailable and unstable bundles", () => {
    for (const note of [
      "overhead could not be estimated: CPU samples were unavailable",
      "the target's untraced CPU differed by 40% between samples, so the overhead estimate is unreliable",
    ]) {
      const quality = {
        frame_pointers_ok: true,
        unknown_frame_ratio: 0,
        estimated_overhead_pct: 0,
        notes: [note],
      };
      expect(usableOverheadPct(quality)).toBeNull();
      expect(formatOverheadPct(quality)).toBe("n/a");
    }
  });

  it("preserves a measured zero", () => {
    const quality = {
      frame_pointers_ok: true,
      unknown_frame_ratio: 0,
      estimated_overhead_pct: 0,
      overhead: { cpu_pct_before: 100, cpu_pct_during: 90 },
    };
    expect(usableOverheadPct(quality)).toBe(0);
    expect(formatOverheadPct(quality)).toBe("0.0%");
  });
});

describe("old run queue histograms", () => {
  function bundleWithBucket(lo: number): Bundle {
    const histogram = {
      schema_version: 1,
      kind: "histogram",
      name: "runqlat",
      unit: "us",
      series: [{ key: "", buckets: [{ lo, hi: lo * 2, count: 1 }] }],
    };
    return {
      key: "test",
      fileName: "test.tgz",
      manifest: manifest({ schema_version: 2, duration_s: 45 }),
      system: null,
      threads: null,
      files: new Map([[PATHS.runqlat, new TextEncoder().encode(JSON.stringify(histogram))]]),
      loadedAt: 0,
    };
  }

  it("flags a 35 minute wait in a 45 second capture", () => {
    expect(bundleQualityFlags(bundleWithBucket(2_147_483_648)).some(
      (flag) => flag.code === "runqlat_unreliable" && flag.level === "error",
    )).toBe(true);
  });

  it("keeps plausible waits available", () => {
    expect(bundleQualityFlags(bundleWithBucket(32)).some(
      (flag) => flag.code === "runqlat_unreliable",
    )).toBe(false);
  });

  it("flags legacy system-wide data even with plausible waits", () => {
    const bundle = bundleWithBucket(32);
    bundle.manifest.schema_version = 1;
    expect(bundleQualityFlags(bundle).some(
      (flag) => flag.message.includes("system-wide"),
    )).toBe(true);
  });

  it("reports an unreadable artifact without crashing", () => {
    for (const body of ["[]", "{", "null"]) {
      const bundle = bundleWithBucket(32);
      bundle.files.set(PATHS.runqlat, new TextEncoder().encode(body));
      expect(bundleQualityFlags(bundle).some(
        (flag) => flag.code === "runqlat_unreadable",
      )).toBe(true);
    }
  });
});

describe("stacksAreTrustworthy", () => {
  it("is false exactly when the viewer should refuse to argue from the graph", () => {
    expect(stacksAreTrustworthy(manifest())).toBe(true);
    expect(
      stacksAreTrustworthy(
        manifest({
          quality: {
            frame_pointers_ok: true,
            unknown_frame_ratio: 0.4,
            estimated_overhead_pct: 0,
          },
        }),
      ),
    ).toBe(false);
  });
});
