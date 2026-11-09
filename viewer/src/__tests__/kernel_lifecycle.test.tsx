import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { Bundle } from "../bundle/load";
import { PATHS, type TableDoc } from "../bundle/types";
import { Kernel } from "../screens/Kernel";
import { Threads } from "../screens/Threads";
import { DataTable } from "../components/DataTable";

function bundle(): Bundle {
  return {
    key: "test", fileName: "test.tgz", loadedAt: 0, system: null, threads: null, files: new Map(),
    manifest: {
      schema_version: 2, run_id: "test", label: "test", started_at: "2026-11-10T00:00:00Z",
      duration_s: 20, profile: "standard", status: "ok", tool_versions: {}, probes: [],
      target: { pid: 1, comm: "app", thread_count_start: 0, thread_count_end: 0 },
      quality: { frame_pointers_ok: true, unknown_frame_ratio: 0, estimated_overhead_pct: null },
    },
  };
}
function put(run: Bundle, path: string, value: unknown) {
  run.files.set(path, new TextEncoder().encode(JSON.stringify(value)));
}

describe("optional kernel and lifecycle payloads", () => {
  it("shows short-lived thread events without any process snapshot", () => {
    const run = bundle();
    put(run, PATHS.threadEvents, { schema_version: 2, kind: "table", name: "thread_events",
      columns: [{ id: "timestamp_ns", type: "string" }, { id: "tid", type: "int" }, { id: "event", type: "string" }],
      rows: [["10000000000000001", 4567, "created"], ["10000000000000002", 4567, "exited"]], truncated: true,
    });
    const html = renderToStaticMarkup(<Threads bundle={run} />);
    expect(html).toContain("4567");
    expect(html).toContain("10000000000000001");
    expect(html).toContain("created");
    expect(html).toContain("exited");
    expect(html).toContain("This list is incomplete");
  });
  it("does not pretend old bundles contain an event log", () => {
    expect(renderToStaticMarkup(<Threads bundle={bundle()} />)).toContain("no individual lifecycle event records");
    expect(renderToStaticMarkup(<Kernel bundle={bundle()} />)).toContain("No kernel module tracing was requested");
  });
  it("labels module results as system-wide elapsed durations and shows probe failures", () => {
    const run = bundle();
    put(run, PATHS.kernelTrace, { scope: "system-wide", module: "my_driver", functions: ["my_work"], tracepoints: [], requests: [] });
    run.manifest.probes.push({ name: "kernel", status: "failed", warnings: ["could not attach return probe"] });
    const html = renderToStaticMarkup(<Kernel bundle={run} />);
    expect(html).toContain("my_driver");
    expect(html).toContain("not attributed to the target PID");
    expect(html).toContain("inclusive elapsed durations");
    expect(html).toContain("could not attach return probe");
    expect(html).toContain("does not contain this measurement");
  });
  it("bounds initial rendering of a large event log", () => {
    const document: TableDoc = { schema_version: 2, kind: "table", name: "events", columns: [{ id: "tid", type: "int" }], rows: Array.from({ length: 2048 }, (_, n) => [n]) };
    const html = renderToStaticMarkup(<DataTable document={document} />);
    expect(html).toContain("page 1/21");
    expect((html.match(/<tr/g) ?? []).length).toBe(101);
  });
});
