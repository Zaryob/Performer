import { useState } from "react";
import type { TableDoc } from "../bundle/types";
import { Empty } from "./ui";

/** Bounded, searchable table for optional event payloads and probe results. */
export function DataTable({ document, empty = "No retained events or results in this capture window." }: {
  document: TableDoc | null; empty?: string;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  if (!document) return <Empty>This bundle does not contain this measurement.</Empty>;
  const needle = query.toLowerCase();
  const rows = document.rows.filter((row) => !needle || row.some((cell) => String(cell ?? "").toLowerCase().includes(needle)));
  const pages = Math.max(1, Math.ceil(rows.length / 100));
  const current = Math.min(page, pages - 1);
  return <div className="space-y-3">
    {document.truncated && <p role="alert" className="text-sm text-amber-200">Some events were not retained. This list is incomplete; see the probe warnings.</p>}
    <div className="flex flex-wrap items-center justify-between gap-2">
      <input aria-label={`Filter ${document.name}`} value={query}
        onChange={(event) => { setQuery(event.target.value); setPage(0); }}
        placeholder="Filter events or results"
        className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm" />
      <span className="text-xs text-slate-400">{rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"} · page {current + 1}/{pages}</span>
    </div>
    {!rows.length ? <Empty>{query ? "No matching rows." : empty}</Empty> : <div className="overflow-x-auto">
      <table className="w-full whitespace-nowrap text-left text-xs">
        <thead className="text-slate-400"><tr>{document.columns.map((column) =>
          <th key={column.id} className="px-2 py-2 font-medium">{column.label ?? column.id.replaceAll("_", " ")}{column.unit && column.unit !== "none" && !column.label ? ` (${column.unit})` : ""}</th>
        )}</tr></thead>
        <tbody>{rows.slice(current * 100, (current + 1) * 100).map((row, index) => <tr key={index} className="border-t border-slate-800">
          {document.columns.map((column, cellIndex) => <td key={column.id} className="px-2 py-2 font-mono text-slate-200">{
            row[cellIndex] === null || row[cellIndex] === undefined ? "—" :
              typeof row[cellIndex] === "number" && column.type === "float" ? Number(row[cellIndex]).toFixed(3) : String(row[cellIndex])
          }</td>)}
        </tr>)}</tbody>
      </table>
    </div>}
    {pages > 1 && <div className="flex gap-3 text-sm">
      <button disabled={current === 0} onClick={() => setPage(current - 1)} className="disabled:opacity-30">Previous</button>
      <button disabled={current + 1 === pages} onClick={() => setPage(current + 1)} className="disabled:opacity-30">Next</button>
    </div>}
  </div>;
}
