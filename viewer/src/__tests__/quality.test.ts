import { describe, expect, it } from "vitest";
import { qualityFlags, stacksAreTrustworthy, worstLevel, THRESHOLDS } from "../quality";
import type { Manifest } from "../bundle/types";

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
