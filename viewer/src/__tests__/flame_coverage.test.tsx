import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Bundle } from "../bundle/load";
import { PATHS, type ThreadsDoc } from "../bundle/types";
import { Flame } from "../screens/Flame";

function bundle(stacks: string | null, count = 121): Bundle {
  const threads: ThreadsDoc = {
    schema_version: 2,
    threads: Object.fromEntries(Array.from({ length: count }, (_, index) => [
      String(index + 100), { name: "worker" },
    ])),
  };
  return {
    key: "test",
    fileName: "test.tgz",
    manifest: {
      schema_version: 2,
      run_id: "test",
      label: "test",
      started_at: "2026-10-01T00:00:00Z",
      duration_s: 30,
      profile: "standard",
      status: "ok",
      target: { pid: 100, comm: "worker", thread_count_start: count, thread_count_end: count },
      probes: [],
      tool_versions: {},
      quality: { frame_pointers_ok: true, unknown_frame_ratio: 0, estimated_overhead_pct: null },
    },
    threads,
    system: null,
    files: stacks === null ? new Map() : new Map([[PATHS.oncpu, new TextEncoder().encode(stacks)]]),
    loadedAt: 0,
  };
}

describe("Flame thread coverage", () => {
  it("distinguishes 121 inventoried threads from 120 sampled TIDs", () => {
    const stacks = Array.from({ length: 120 }, (_, index) =>
      `worker [tid=${index + 100}];run 1`,
    ).join("\n");
    const html = renderToStaticMarkup(<Flame bundle={bundle(stacks)} />);
    expect(html).toContain("121 threads in /proc inventory");
    expect(html).toContain("120 TIDs with recorded stacks");
    expect(html).toContain("1 inventory threads have no On-CPU stacks");
    expect(html).toContain("no On-CPU stack captured");
    expect(html).not.toContain("merged into one tree");
  });

  it("lists all 315 inventory rows, including those without samples", () => {
    const html = renderToStaticMarkup(<Flame bundle={bundle("worker [tid=100];run 3", 315)} />);
    expect(html).toContain("315 of 315 rows");
    expect(html.match(/<tr /g)).toHaveLength(315);
    expect(html).toContain(">414</td>");
    expect(html).not.toContain("200 busiest");
  });

  it("does not label legacy name aggregates as sampled threads", () => {
    const html = renderToStaticMarkup(<Flame bundle={bundle("worker;run 30")} />);
    expect(html).toContain("0 TIDs with recorded stacks");
    expect(html).toContain("1 legacy name aggregates");
    expect(html).toContain("TID unavailable in legacy stacks");
    expect(html).toContain("values cannot be assigned to individual TIDs");
    expect(html).not.toContain("inventory threads have no On-CPU stacks");
  });

  it("keeps the inventory when a probe produces no stack file", () => {
    const html = renderToStaticMarkup(<Flame bundle={bundle(null)} />);
    expect(html).toContain("This run has no stack files");
    expect(html).toContain("121 of 121 rows");
    expect(html).toContain("A dash means no attributable stack measurement");
  });
});
