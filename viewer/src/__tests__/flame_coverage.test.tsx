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

  it("explains the difference between TIDs in data and narrow roots on canvas", () => {
    const stacks = Array.from({ length: 120 }, (_, index) =>
      `worker [tid=${index + 100}];run ${index < 10 ? 1000 : 1}`,
    ).join("\n");
    const html = renderToStaticMarkup(<Flame bundle={bundle(stacks)} />);
    expect(html).toContain("120 TIDs in graph data");
    expect(html).toContain("10 thread roots drawn at this width");
    expect(html).toContain("110 TIDs have roots narrower than");
    expect(html).toContain("1.088% of graph data");
    expect(html).toContain("show this thread");
    expect(html.match(/show this thread<\/button>/g)).toHaveLength(120);
  });

  it("shows independent CPU deltas without assuming the snapshot and tracing windows match", () => {
    const run = bundle("worker [tid=100];run 3", 3);
    const schedstat = (run_ns: number) => ({ run_ns, wait_ns: 0, timeslices: 0 });
    run.threads!.threads["101"]!.start_schedstat = schedstat(100);
    run.threads!.threads["101"]!.end_schedstat = schedstat(8_000_100);
    run.threads!.threads["102"]!.start_schedstat = schedstat(100);
    run.threads!.threads["102"]!.end_schedstat = schedstat(100);
    const html = renderToStaticMarkup(<Flame bundle={run} />);
    expect(html).toContain("1 threads accumulated CPU time");
    expect(html).toContain("snapshot window may extend beyond tracing");
    expect(html).not.toContain("sampling coverage gap");
    expect(html).toContain("8.000</td>");
    expect(html).toContain("0.000</td>");
    expect(html).toContain("Comparable runtime snapshots unavailable");
  });

  it("surfaces selected stack probe coverage warnings", () => {
    const run = bundle("worker [tid=100];run 3", 3);
    run.manifest.probes = [{ name: "oncpu", status: "partial", thresholds: { sample_hz: 999 }, warnings: ["Sampling covered only part of the collection."] }];
    const html = renderToStaticMarkup(<Flame bundle={run} />);
    expect(html).toContain("Sampling covered only part of the collection.");
    expect(html).toContain("999 Hz per CPU");
  });

  it("shows incomplete Off-CPU interval warnings with the Off-CPU graph", () => {
    const run = bundle(null, 3);
    run.files.set(PATHS.offcpu, new TextEncoder().encode("worker [tid=100];poll 100000"));
    run.manifest.probes = [{
      name: "offcpu", status: "ok",
      warnings: ["Included right-censored off-CPU intervals; full waits are unknown."],
    }];
    const html = renderToStaticMarkup(<Flame bundle={run} />);
    expect(html).toContain("Included right-censored off-CPU intervals; full waits are unknown.");
    expect(html).toContain("plus observed open waits up to the last tracing checkpoint");
    expect(html).toContain("The duration histogram includes completed waits only");
    expect(html).toContain("no Off-CPU stack captured");
    expect(html).not.toContain("sampling coverage gap");
  });
});
