import type { Bundle } from "../bundle/load";
import { readJson } from "../bundle/load";
import { PATHS, type TableDoc } from "../bundle/types";
import { DataTable } from "./DataTable";
import { Panel } from "./ui";

export function ThreadLifecycle({ bundle }: { bundle: Bundle }) {
  const events = readJson<TableDoc>(bundle, PATHS.threadEvents);
  if (!events) return <Panel title="Thread lifecycle events"><p className="text-sm text-slate-400">
    This bundle has no individual lifecycle event records. Older bundles may contain aggregate counts only.
  </p></Panel>;
  const probe = bundle.manifest.probes.find((item) => item.name === "threadlife");
  return <Panel title="Thread lifecycle events">
    <p className="mb-3 text-sm text-slate-400">
      Includes short-lived threads absent from both process snapshots. Created/exited events retain TID,
      creator TID and the observed birth timestamp to distinguish reused TIDs. Unknown births and parents
      stay empty. Names are those observed at the event and may change. Lifetime is shown only when birth was observed.
    </p>
    {probe?.warnings?.map((warning, index) => <p key={index} className="mb-2 text-sm text-amber-200">{warning}</p>)}
    <DataTable key={bundle.key} document={events} />
  </Panel>;
}
