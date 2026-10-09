/**
 * "New measurement": start a collection from the browser.
 *
 * Only reachable when the daemon is serving this page, because `file://`
 * cannot call an API at all. That constraint turns out to be the right
 * product decision too — the collection controls are absent rather than
 * greyed out on the analysis machine where they could never work.
 *
 * The screen is arranged around the two things that go wrong: choosing the
 * wrong process, and choosing a profile too expensive for the box. So targets
 * are listed with their thread counts rather than typed as a pid, and each
 * profile carries its own overhead and duration ceiling next to the control
 * that would exceed it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  fetchBundle,
  type DaemonStatus,
  type JobInfo,
  type ProfileInfo,
  type Target,
} from "../api";
import { bundleFromArchive, type Bundle } from "../bundle/load";
import { Empty, Panel, formatDuration } from "../components/ui";

const POLL_MS = 700;

export function Collect({
  status,
  onBundle,
}: {
  status: DaemonStatus;
  onBundle: (bundle: Bundle) => void;
}) {
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [filter, setFilter] = useState("");
  const [pid, setPid] = useState<number | null>(null);
  const [profileName, setProfileName] = useState<string>("");
  const [duration, setDuration] = useState(60);
  const [oncpuHz, setOncpuHz] = useState(99);
  const [pmu, setPmu] = useState<"off" | "basic">("off");
  const [label, setLabel] = useState("");
  const [notes, setNotes] = useState("");
  const [job, setJob] = useState<JobInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [opened, setOpened] = useState<string | null>(null);

  const refreshTargets = useCallback(async () => {
    try {
      const [profileDoc, targetDoc] = await Promise.all([
        api.profiles(),
        api.targets(),
      ]);
      setProfiles(profileDoc.profiles);
      setTargets(targetDoc.targets);
      setProfileName((current) => current || profileDoc.profiles[0]?.name || "");
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshTargets();
  }, [refreshTargets]);

  const profile = profiles.find((entry) => entry.name === profileName) ?? null;

  // Clamping rather than validating on submit: an operator should not be able
  // to type a number the daemon will refuse, and the ceiling is a property of
  // the profile they just chose.
  useEffect(() => {
    if (profile && duration > profile.max_duration_s) {
      setDuration(profile.max_duration_s);
    }
  }, [profile, duration]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return targets;
    return targets.filter(
      (target) =>
        target.comm.toLowerCase().includes(needle) ||
        String(target.pid).includes(needle) ||
        (target.cmdline.join(" ").toLowerCase().includes(needle)),
    );
  }, [targets, filter]);

  const selected = targets.find((target) => target.pid === pid) ?? null;
  const running = job !== null && (job.state === "queued" || job.state === "running");

  // -- polling ---------------------------------------------------------
  const jobId = job?.id ?? null;
  const timer = useRef<number | null>(null);
  useEffect(() => {
    if (!jobId || !running) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await api.job(jobId);
        if (!cancelled) setJob(next);
      } catch (caught) {
        if (!cancelled) setError((caught as Error).message);
      }
    };
    timer.current = window.setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      if (timer.current !== null) window.clearInterval(timer.current);
    };
  }, [jobId, running]);

  // When a run finishes, open it here rather than making anyone find a file.
  useEffect(() => {
    if (!job || job.state !== "done" || !job.run_id || opened === job.id) return;
    setOpened(job.id);
    void (async () => {
      try {
        const bytes = await fetchBundle(job.run_id as string);
        onBundle(bundleFromArchive(`${job.run_id}.tgz`, bytes));
      } catch (caught) {
        setError(`collected, but could not open it: ${(caught as Error).message}`);
      }
    })();
  }, [job, opened, onBundle]);

  const start = async () => {
    if (pid === null || !profile) return;
    setError(null);
    try {
      setJob(await api.collect({
        pid,
        profile: profile.name,
        label: label.trim(),
        duration_s: duration,
        pmu,
        oncpu_hz: oncpuHz,
        notes: notes.trim() || undefined,
      }));
      setOpened(null);
    } catch (caught) {
      const message =
        caught instanceof ApiError && caught.status === 409
          ? `${caught.message}`
          : (caught as Error).message;
      setError(message);
    }
  };

  const labelProblem = label && !/^[A-Za-z0-9._-]{1,64}$/.test(label)
    ? "letters, digits, dot, dash and underscore only — it becomes a directory name"
    : null;

  if (loading) return <Empty>Asking the daemon what it can do…</Empty>;

  return (
    <div className="space-y-4">
      {!status.can_collect && (
        <div className="rounded border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
          <span className="mr-2 font-bold">!</span>
          {status.tool_issues.length > 0
            ? `Collection is unavailable: ${status.tool_issues.join("; ")}`
            : <>This daemon is not running as root. Restart it with <code>sudo</code> to collect.</>}
        </div>
      )}
      {profile && profile.tool_issues.length > 0 && (
        <div className="rounded border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
          This profile cannot run: {profile.tool_issues.join("; ")}
        </div>
      )}
      {error && (
        <div className="rounded border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          <span className="mr-2 font-bold">✕</span>
          {error}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[3fr_2fr]">
        <Panel
          title="Target process"
          right={
            <div className="flex items-center gap-2">
              <input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="filter by name or pid"
                aria-label="filter targets"
                className="w-48 rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
              />
              <button
                type="button"
                onClick={() => void refreshTargets()}
                className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600"
              >
                refresh
              </button>
            </div>
          }
        >
          {visible.length === 0 ? (
            <Empty>No multi-threaded processes match.</Empty>
          ) : (
            <div className="max-h-96 overflow-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="py-1 pr-3">process</th>
                    <th className="py-1 pr-3 text-right">pid</th>
                    <th className="py-1 pr-3 text-right">threads</th>
                    <th className="py-1">command</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((target) => (
                    <tr
                      key={target.pid}
                      onClick={() => {
                        setPid(target.pid);
                        if (!label) setLabel(sanitiseLabel(target.comm));
                      }}
                      className={`cursor-pointer border-t border-slate-800 ${
                        target.pid === pid
                          ? "bg-sky-500/15"
                          : "hover:bg-slate-800/40"
                      }`}
                    >
                      <td className="py-1 pr-3 font-mono text-slate-100">
                        {target.comm}
                      </td>
                      <td className="py-1 pr-3 text-right tabular-nums text-slate-400">
                        {target.pid}
                      </td>
                      <td
                        className={`py-1 pr-3 text-right tabular-nums ${
                          target.threads >= 100 ? "text-amber-300" : "text-slate-300"
                        }`}
                      >
                        {target.threads.toLocaleString()}
                      </td>
                      <td
                        className="max-w-0 truncate py-1 font-mono text-xs text-slate-500"
                        title={target.cmdline.join(" ")}
                      >
                        {target.cmdline.join(" ") || target.exe || ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel title="Measurement">
          <div className="space-y-3">
            <label className="flex flex-col gap-1 text-xs text-slate-400">
              profile
              <select
                aria-label="profile"
                value={profileName}
                onChange={(event) => setProfileName(event.target.value)}
                className="rounded border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100"
              >
                {profiles.map((entry) => (
                  <option key={entry.name} value={entry.name}>
                    {entry.name} — {entry.expected_overhead}
                  </option>
                ))}
              </select>
            </label>
            {profile && (
              <p className="text-xs text-slate-500">
                {profile.description}
                <br />
                {profile.probes.length} probes: {profile.probes.join(", ")} · at
                most {formatDuration(profile.max_duration_s)}
              </p>
            )}

            <label className="flex flex-col gap-1 text-xs text-slate-400">
              on-CPU sampling
              <select
                aria-label="on-CPU sampling"
                value={oncpuHz}
                onChange={(event) => setOncpuHz(Number(event.target.value))}
                className="rounded border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100"
              >
                <option value={99}>99 Hz · default</option>
                <option value={499}>499 Hz</option>
                <option value={999}>999 Hz</option>
              </select>
              {oncpuHz > 99 && <span className="text-amber-200">More samples can reveal short bursts, but increase collection cost.</span>}
            </label>

            <label className="flex flex-col gap-1 text-xs text-slate-400">
              hardware counters
              <select
                aria-label="hardware counters"
                value={pmu}
                onChange={(event) => setPmu(event.target.value as "off" | "basic")}
                className="rounded border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100"
              >
                <option value="off">off</option>
                <option value="basic">basic · cycles, instructions, branches, cache</option>
              </select>
            </label>

            <label className="flex flex-col gap-1 text-xs text-slate-400">
              duration — {formatDuration(duration)}
              <input
                type="range"
                aria-label="duration"
                min={5}
                max={profile?.max_duration_s ?? 300}
                step={5}
                value={duration}
                onChange={(event) => setDuration(Number(event.target.value))}
              />
            </label>

            <label className="flex flex-col gap-1 text-xs text-slate-400">
              label
              <input
                value={label}
                aria-label="label"
                onChange={(event) => setLabel(event.target.value)}
                placeholder="e.g. before-fix"
                className={`rounded border bg-slate-950 px-2 py-1 text-sm text-slate-100 ${
                  labelProblem ? "border-red-500" : "border-slate-600"
                }`}
              />
              {labelProblem && (
                <span className="text-red-300">{labelProblem}</span>
              )}
            </label>

            <label className="flex flex-col gap-1 text-xs text-slate-400">
              notes (optional)
              <textarea
                value={notes}
                aria-label="notes"
                onChange={(event) => setNotes(event.target.value)}
                rows={2}
                placeholder="what changed since the last run"
                className="rounded border border-slate-600 bg-slate-950 px-2 py-1 text-sm text-slate-100"
              />
            </label>

            <button
              type="button"
              onClick={() => void start()}
              disabled={
                pid === null || !label || Boolean(labelProblem) || running ||
                !status.can_collect || Boolean(profile?.tool_issues.length)
              }
              className="w-full rounded bg-sky-600 px-3 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
            >
              {running
                ? "measuring…"
                : selected
                  ? `Measure ${selected.comm} (pid ${selected.pid}) for ${formatDuration(duration)}`
                  : "Choose a process"}
            </button>
            {status.busy && !running && (
              <p className="text-xs text-amber-300">
                Another measurement is already running on this daemon.
              </p>
            )}
          </div>
        </Panel>
      </div>

      {job && <JobPanel job={job} onCancel={() => void api.cancel(job.id).then(setJob)} />}
    </div>
  );
}

function JobPanel({ job, onCancel }: { job: JobInfo; onCancel: () => void }) {
  const logRef = useRef<HTMLPreElement | null>(null);
  useEffect(() => {
    const element = logRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [job.log]);

  const running = job.state === "queued" || job.state === "running";
  const progress =
    job.elapsed_s !== null && job.duration_s > 0
      ? Math.min(1, job.elapsed_s / job.duration_s)
      : 0;

  return (
    <Panel
      title={`Job ${job.id} — ${job.state}`}
      right={
        running ? (
          <button
            type="button"
            onClick={onCancel}
            className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600"
          >
            stop and keep what has been collected
          </button>
        ) : (
          <span className="text-xs text-slate-400">
            {job.run_id ?? job.error ?? ""}
          </span>
        )
      }
    >
      {running && (
        <div className="mb-2 h-1 w-full overflow-hidden rounded bg-slate-800">
          {/* Preflight happens before the clock starts, so this bar tracks the
              collection rather than the whole job — it is honest about which. */}
          <div
            className="h-full bg-sky-500 transition-[width] duration-500"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      )}
      <p className="mb-2 text-xs text-slate-400">
        pid {job.pid} · {job.profile} · {formatDuration(job.duration_s)} requested
        {job.elapsed_s !== null && <> · {job.elapsed_s.toFixed(0)} s elapsed</>}
        {job.status && <> · bundle status {job.status}</>}
      </p>
      {job.error && (
        <p className="mb-2 rounded border border-red-500/50 bg-red-500/10 px-2 py-1 text-sm text-red-200">
          {job.error}
        </p>
      )}
      <pre
        ref={logRef}
        className="max-h-64 overflow-auto rounded bg-slate-950 p-2 font-mono text-xs leading-relaxed text-slate-300"
      >
        {job.log.join("\n") || "…"}
      </pre>
      {job.state === "done" && (
        <p className="mt-2 text-sm text-emerald-300">
          Collected and opened — it is on the Runs screen.
        </p>
      )}
    </Panel>
  );
}

/** Turn a process name into something `layout.LABEL_RE` will accept. */
function sanitiseLabel(comm: string): string {
  return comm.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 64) || "run";
}
