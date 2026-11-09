import type { Bundle } from "../bundle/load";
import { readJson } from "../bundle/load";
import { PATHS, type TableDoc } from "../bundle/types";
import { DataTable } from "../components/DataTable";
import { Empty, Panel, StatusBadge } from "../components/ui";

interface KernelConfig {
  scope: "system-wide";
  module: string;
  functions: string[];
  tracepoints: string[];
  requests: { name: string; start: string; end: string; key: string }[];
}

export function Kernel({ bundle }: { bundle: Bundle }) {
  const config = readJson<KernelConfig>(bundle, PATHS.kernelTrace);
  if (!config) return <Empty>No kernel module tracing was requested in this run. Collect with --kernel-config to add it.</Empty>;
  const probe = bundle.manifest.probes.find((item) => item.name === "kernel");
  return <div className="space-y-4">
    <Panel title={`Kernel module: ${config.module}`} right={probe ? <StatusBadge status={probe.status} /> : undefined}>
      <p className="text-sm text-amber-100">System-wide module/event selection. These results include other processes, kernel workers and interrupt contexts; they are not attributed to the target PID.</p>
      <p className="mt-2 text-sm text-slate-400">Times are inclusive elapsed durations of calls or request pairs whose start and end were both observed in the capture. They include scheduling and interruptions, and are not exclusive CPU time. Nested calls may overlap. Entries without a matching endpoint may cross a capture boundary or reflect missing events.</p>
      {probe?.warnings?.map((warning, index) => <p key={index} className="mt-2 text-sm text-amber-200">{warning}</p>)}
    </Panel>
    {config.functions.length > 0 && <Panel title="Function calls"><DataTable key={bundle.key + "functions"} document={readJson<TableDoc>(bundle, PATHS.kernelFunctions)} empty="No selected function calls observed." /></Panel>}
    {config.requests.length > 0 && <Panel title="Request completion latency">
      <p className="mb-3 text-sm text-slate-400">Correlated by the configured integer request key, including completion in another execution context. IDs must be unique among outstanding requests in each named pair.</p>
      <DataTable key={bundle.key + "requests"} document={readJson<TableDoc>(bundle, PATHS.kernelRequests)} empty="No request endpoints observed." />
    </Panel>}
    {config.tracepoints.length > 0 && <Panel title="Tracepoint counts"><DataTable key={bundle.key + "tracepoints"} document={readJson<TableDoc>(bundle, PATHS.kernelTracepoints)} /></Panel>}
    {(config.requests.length > 0 || config.tracepoints.length > 0) && <Panel title="Kernel events"><DataTable key={bundle.key + "events"} document={readJson<TableDoc>(bundle, PATHS.kernelEvents)} /></Panel>}
  </div>;
}
