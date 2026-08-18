/**
 * Talking to the collector, when there is one to talk to.
 *
 * The viewer's whole point is that `dist/index.html` opens by double click on
 * a machine with no server, and that stays true: `file://` forbids `fetch`
 * outright, so this module answers "no daemon" and every collection control
 * disappears. The API only exists when the daemon is *serving* this page,
 * which is the one situation where calling it is both possible and correct.
 *
 * The token never goes in a cookie. It arrives once in the query string, is
 * moved straight into `sessionStorage`, and is stripped from the URL so it
 * does not sit in the address bar, in history, or in a screenshot. Every
 * request sends it in an `Authorization` header, which a page on another
 * origin cannot forge and cannot read the response to.
 */

const TOKEN_KEY = "performer.token";

export interface Health {
  ok: boolean;
  performer: string;
}

export interface DaemonStatus {
  performer: string;
  schema_version: number;
  uptime_s: number;
  out_dir: string;
  profiles: string[];
  busy: boolean;
  current_job: string | null;
  can_collect: boolean;
  tool_issues: string[];
  viewer: boolean;
}

export interface ProfileInfo {
  name: string;
  description: string;
  probes: string[];
  max_duration_s: number;
  expected_overhead: string;
  tool_issues: string[];
}

export interface Target {
  pid: number;
  comm: string;
  threads: number;
  cmdline: string[];
  exe: string | null;
}

export interface RunSummary {
  run_id: string;
  label: string;
  status: string;
  profile: string;
  started_at: string;
  duration_s: number;
  comm: string | null;
  pid: number | null;
  bytes: number;
}

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled";

export interface JobInfo {
  id: string;
  pid: number;
  profile: string;
  label: string;
  duration_s: number;
  state: JobState;
  started_at: number | null;
  ended_at: number | null;
  elapsed_s: number | null;
  log: string[];
  error: string | null;
  run_id: string | null;
  archive: string | null;
  status: string | null;
}

export interface CollectRequest {
  pid: number;
  profile: string;
  label: string;
  duration_s: number;
  tags?: string[];
  notes?: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** True when this page was served by the daemon rather than opened as a file. */
export function servedOverHttp(): boolean {
  return (
    typeof window !== "undefined" &&
    (window.location.protocol === "http:" || window.location.protocol === "https:")
  );
}

/**
 * Take the token out of the URL and keep it for the session.
 *
 * `replaceState` rather than leaving it: a URL in the address bar gets
 * screenshotted, pasted into chat, and saved in history, and this one starts
 * privileged measurements.
 */
export function adoptToken(): string | null {
  if (typeof window === "undefined") return null;
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get("token");
  if (fromQuery) {
    sessionStorage.setItem(TOKEN_KEY, fromQuery);
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromQuery;
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

export function currentToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(TOKEN_KEY);
}

async function call<T>(
  path: string,
  init: RequestInit = {},
  { authenticated = true } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (authenticated) {
    const token = currentToken();
    if (!token) throw new ApiError("no token", 401);
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body !== undefined) headers.set("Content-Type", "application/json");

  const response = await fetch(path, {
    ...init,
    headers,
    // No cookies, ever: the token is the only credential and it travels in a
    // header a cross-origin page cannot set.
    credentials: "omit",
  });
  const text = await response.text();
  let document: unknown = null;
  try {
    document = text ? JSON.parse(text) : null;
  } catch {
    document = null;
  }
  if (!response.ok) {
    const message =
      (document as { error?: string } | null)?.error ??
      `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status);
  }
  return document as T;
}

/** Is a daemon there at all? Answers false rather than throwing. */
export async function probe(): Promise<Health | null> {
  if (!servedOverHttp()) return null;
  try {
    return await call<Health>("/api/health", {}, { authenticated: false });
  } catch {
    return null;
  }
}

export const api = {
  status: () => call<DaemonStatus>("/api/status"),
  profiles: () => call<{ profiles: ProfileInfo[] }>("/api/profiles"),
  targets: () => call<{ targets: Target[] }>("/api/targets"),
  runs: () => call<{ runs: RunSummary[] }>("/api/runs"),
  jobs: () => call<{ jobs: JobInfo[] }>("/api/jobs"),
  job: (id: string) => call<JobInfo>(`/api/jobs/${encodeURIComponent(id)}`),
  collect: (request: CollectRequest) =>
    call<JobInfo>("/api/collect", {
      method: "POST",
      body: JSON.stringify(request),
    }),
  cancel: (id: string) =>
    call<JobInfo>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
};

/**
 * Fetch a finished bundle so it can be opened without a round trip through
 * the file system.
 *
 * This is the point of serving the viewer at all: measure and look, in one
 * place, without a download folder in between.
 */
export async function fetchBundle(runId: string): Promise<Uint8Array> {
  const token = currentToken();
  if (!token) throw new ApiError("no token", 401);
  const response = await fetch(
    `/api/runs/${encodeURIComponent(runId)}/bundle`,
    { headers: { Authorization: `Bearer ${token}` }, credentials: "omit" },
  );
  if (!response.ok) {
    throw new ApiError(`could not download run ${runId}`, response.status);
  }
  return new Uint8Array(await response.arrayBuffer());
}
