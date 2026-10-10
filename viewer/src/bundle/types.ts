/**
 * The bundle format, as TypeScript.
 *
 * These mirror `schema/*.json`. The collector validates every document it
 * writes against those schemas before the bundle exists, so the viewer can
 * treat a well-formed bundle as trustworthy -- but it still checks
 * `schema_version`, because a bundle from a future collector is exactly the
 * case where guessing would produce a confident wrong answer.
 */

export const SUPPORTED_SCHEMA_VERSION = 2;
export const MIN_SUPPORTED_SCHEMA_VERSION = 1;

export type ProbeStatus = "ok" | "partial" | "failed" | "skipped";
export type RunStatus = "ok" | "partial" | "failed";

export interface ProbeResult {
  name: string;
  status: ProbeStatus;
  events_lost?: number;
  warnings?: string[];
  duration_s?: number;
  exit_reason?: string;
  exit_code?: number | null;
  thresholds?: Record<string, number | string | boolean>;
  outputs?: string[];
}

export interface TargetInfo {
  pid: number;
  comm: string;
  cmdline?: string[];
  exe?: string | null;
  thread_count_start: number;
  thread_count_end: number;
}

export interface Quality {
  frame_pointers_ok: boolean;
  unknown_frame_ratio: number;
  estimated_overhead_pct: number | null;
  unknown_frame_samples?: number;
  total_frame_samples?: number;
  overhead?: {
    cpu_pct_before?: number;
    cpu_pct_during?: number;
    cpu_pct_after?: number;
    sample_window_s?: number;
    other_cpu_cores_reference?: number;
    settle_before_s?: number;
    settle_after_s?: number;
  };
  ignore_quality?: boolean;
  notes?: string[];
}

export interface FileEntry {
  path: string;
  bytes: number;
  sha256?: string;
  rows?: number;
}

export interface Manifest {
  schema_version: number;
  run_id: string;
  label: string;
  tags?: string[];
  notes?: string;
  started_at: string;
  ended_at?: string;
  duration_s: number;
  actual_duration_s?: number;
  profile: string;
  status: RunStatus;
  target_died_at?: string | null;
  target: TargetInfo;
  probes: ProbeResult[];
  tool_versions: Record<string, string | null>;
  quality: Quality;
  files?: FileEntry[];
  warnings?: string[];
}

// -- payload documents ------------------------------------------------------

export interface HistBucket {
  lo: number | null;
  hi: number | null;
  count: number;
}

export interface HistSeries {
  key: string;
  buckets: HistBucket[];
  total_count?: number;
  stats?: Partial<
    Record<"count" | "sum" | "min" | "max" | "avg", number>
  >;
}

export interface HistogramDoc {
  schema_version: number;
  kind: "histogram";
  name: string;
  unit: string;
  source?: string;
  series: HistSeries[];
}

export interface TableColumn {
  id: string;
  label?: string;
  type: "string" | "int" | "float" | "hex" | "stack";
  unit?: string;
  sort?: "asc" | "desc";
}

export type TableCell = string | number | boolean | null;

export interface TableDoc {
  schema_version: number;
  kind: "table";
  name: string;
  source?: string;
  columns: TableColumn[];
  rows: TableCell[][];
  truncated?: boolean;
  total_rows?: number;
}

export type AggregationDoc = HistogramDoc | TableDoc;

export interface WakeupEdge {
  from_tid: number;
  to_tid: number;
  count: number;
  total_us?: number | null;
}

export interface WakeupGraphDoc {
  schema_version: number;
  source?: string;
  nodes?: { tid: number; name?: string; external?: boolean }[];
  edges: WakeupEdge[];
  truncated?: boolean;
  total_edges?: number;
}

export interface Schedstat {
  run_ns: number;
  wait_ns: number;
  timeslices: number;
  voluntary_ctxt_switches?: number;
  nonvoluntary_ctxt_switches?: number;
}

export interface ThreadEntry {
  name: string;
  start_time_ticks?: number;
  first_seen?: "start" | "end";
  exited?: boolean;
  start_schedstat?: Schedstat | null;
  end_schedstat?: Schedstat | null;
}

export interface ThreadsDoc {
  schema_version: number;
  sampled_at_start?: string | null;
  sampled_at_end?: string | null;
  clk_tck?: number;
  threads: Record<string, ThreadEntry>;
}

export interface PmuEvent {
  raw: number;
  scaled: number | null;
  time_enabled_ns: number;
  time_running_ns: number;
}

export interface PmuThread {
  tid: number;
  name: string;
  start_time_ticks: number;
  coverage_s: number;
  events: Record<string, PmuEvent>;
}

export interface PmuDoc {
  schema_version: number;
  kind: "pmu";
  mode: "basic";
  source: string;
  scope: "user";
  status: "ok" | "partial" | "failed";
  cpu_model?: string | null;
  arch?: string;
  window_s: number;
  thread_count_start: number;
  threads_measured: number;
  events: string[];
  totals: Record<string, PmuEvent>;
  threads: PmuThread[];
  warnings: string[];
}

export interface SystemDoc {
  schema_version: number;
  hostname?: string | null;
  kernel: string;
  arch?: string | null;
  distro?: string | null;
  cpu_count: number;
  cpu_model?: string | null;
  mem_total_kb?: number | null;
  perf_event_paranoid?: number | null;
  cgroup?: {
    path?: string | null;
    cpu_max?: string | null;
    memory_max?: string | null;
    cpuset_cpus?: string | null;
  } | null;
  load_avg?: number[] | null;
}

// -- canonical paths, mirroring collector/performer/layout.py ---------------

export const PATHS = {
  manifest: "manifest.json",
  system: "meta/system.json",
  target: "meta/target.json",
  threads: "meta/threads.json",
  pmu: "pmu/counters.json",
  oncpu: "stacks/oncpu.folded",
  offcpu: "stacks/offcpu.folded",
  futex: "stacks/futex.folded",
  offwake: "stacks/offwake.folded",
  runqlat: "hist/runqlat.json",
  offcpuDuration: "hist/offcpu_duration.json",
  offcpuByState: "hist/offcpu_by_state.json",
  futexByAddr: "hist/futex_by_addr.json",
  futexSites: "hist/futex_sites.json",
  futexDuration: "hist/futex_duration.json",
  syscallLatency: "hist/syscall_latency.json",
  threadlife: "hist/threadlife.json",
  threadLifetime: "hist/thread_lifetime.json",
  timers: "hist/timers.json",
  wakeupEdges: "graph/wakeup_edges.json",
  seriesThreads: "series/threads.csv",
  seriesSchedstat: "series/schedstat.csv",
} as const;

/** The three stack files the Flame screen offers, and what their values mean. */
export const STACK_KINDS = [
  { id: "oncpu", path: PATHS.oncpu, label: "On-CPU", unit: "samples" },
  { id: "offcpu", path: PATHS.offcpu, label: "Off-CPU", unit: "µs blocked" },
  { id: "futex", path: PATHS.futex, label: "Futex", unit: "µs waiting" },
] as const;

export type StackKindId = (typeof STACK_KINDS)[number]["id"];
