import { useCallback, useEffect, useMemo, useState } from "react";
import type { Bundle } from "./bundle/load";
import { Runs } from "./screens/Runs";
import { Overview } from "./screens/Overview";
import { Flame } from "./screens/Flame";
import { Threads } from "./screens/Threads";
import { Diff } from "./screens/Diff";
import { Locks } from "./screens/Locks";
import { Wakeups } from "./screens/Wakeups";
import { Collect } from "./screens/Collect";
import { adoptToken, api, probe, setToken, type DaemonStatus } from "./api";
import { Empty } from "./components/ui";

const SCREENS = [
  "Runs",
  "Overview",
  "Flame",
  "Threads",
  "Locks",
  "Wakeups",
  "Diff",
] as const;
type Screen = (typeof SCREENS)[number] | "Collect";

/** Screens still to come, shown so their absence is a plan rather than a gap. */
const PLANNED: { name: string; milestone: string }[] = [
  { name: "Timeline", milestone: "M5" },
];

export function App() {
  const [bundles, setBundles] = useState<Bundle[]>([]);
  // Two selections, not one. `selectedKey` is the run being looked at, which
  // every single-run screen uses; `baselineKey` is what it is being compared
  // against. Keeping the baseline in one place means the Diff screen and the
  // Threads deltas cannot drift into disagreeing about which run is "before".
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [baselineKey, setBaselineKey] = useState<string | null>(null);
  const [screen, setScreen] = useState<Screen>("Runs");

  // The daemon, when there is one. Over `file://` there never is: fetch is
  // forbidden, so `probe()` answers null and every collection control simply
  // does not exist rather than being greyed out on a machine where it could
  // not have worked anyway.
  const [daemon, setDaemon] = useState<DaemonStatus | null>(null);
  const [daemonPresent, setDaemonPresent] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);

  const refreshDaemon = useCallback(async () => {
    const health = await probe();
    setDaemonPresent(health !== null);
    if (!health) return;
    try {
      setDaemon(await api.status());
      setNeedsToken(false);
    } catch {
      setDaemon(null);
      setNeedsToken(true);
    }
  }, []);

  useEffect(() => {
    adoptToken();
    void refreshDaemon();
  }, [refreshDaemon]);

  const selected = useMemo(
    () => bundles.find((bundle) => bundle.key === selectedKey) ?? null,
    [bundles, selectedKey],
  );
  const baseline = useMemo(
    () =>
      baselineKey === selectedKey
        ? null
        : (bundles.find((bundle) => bundle.key === baselineKey) ?? null),
    [bundles, baselineKey, selectedKey],
  );

  /**
   * `navigate` is off when the bundle arrived from a collection the operator
   * is watching: jumping to the Overview would take the job log off screen at
   * the moment it says what happened, and the run is one click away anyway.
   * Dropping a file is the opposite case — there is nothing to stay for.
   */
  const addBundles = (loaded: Bundle[], { navigate = true } = {}) => {
    if (!loaded.length) return;
    setBundles((current) => [...current, ...loaded]);
    const newest = loaded[loaded.length - 1] as Bundle;
    // A run is loaded alongside another one in order to compare it, so pair
    // them up rather than making the operator choose twice. Reading order is
    // the useful convention: what arrived first is "before".
    if (!selectedKey) {
      setSelectedKey(newest.key);
      if (navigate) setScreen("Overview");
      if (loaded.length > 1) setBaselineKey((loaded[0] as Bundle).key);
    } else {
      setBaselineKey(selectedKey);
      setSelectedKey(newest.key);
    }
  };

  const removeBundle = (key: string) => {
    setBundles((current) => current.filter((bundle) => bundle.key !== key));
    if (selectedKey === key) setSelectedKey(null);
    if (baselineKey === key) setBaselineKey(null);
  };

  const swapSides = () => {
    setSelectedKey(baselineKey);
    setBaselineKey(selectedKey);
  };

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-900/80 px-4 py-2 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-4">
          <span className="text-sm font-semibold tracking-wide text-slate-100">
            Performer
          </span>
          <nav className="flex gap-1">
            {SCREENS.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setScreen(name)}
                disabled={
                  name === "Diff" ? bundles.length < 2 : name !== "Runs" && !selected
                }
                title={
                  name === "Diff" && bundles.length < 2
                    ? "load a second run bundle to compare"
                    : undefined
                }
                className={`rounded px-3 py-1 text-sm transition disabled:cursor-not-allowed disabled:opacity-40 ${
                  screen === name
                    ? "bg-sky-600 text-white"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                {name}
              </button>
            ))}
            {PLANNED.map((item) => (
              <span
                key={item.name}
                title={`arrives in ${item.milestone}`}
                className="rounded px-3 py-1 text-sm text-slate-600"
              >
                {item.name}
              </span>
            ))}
            {daemon && (
              <button
                type="button"
                onClick={() => setScreen("Collect")}
                className={`ml-2 rounded px-3 py-1 text-sm transition ${
                  screen === "Collect"
                    ? "bg-emerald-600 text-white"
                    : "bg-emerald-600/20 text-emerald-200 hover:bg-emerald-600/40"
                }`}
              >
                + New measurement
              </button>
            )}
          </nav>
          {selected && (
            <span className="ml-auto text-xs text-slate-400">
              viewing <span className="text-slate-200">{selected.manifest.label}</span>{" "}
              · {selected.manifest.run_id}
              {baseline && (
                <>
                  {" "}
                  · vs{" "}
                  <span className="text-slate-300">{baseline.manifest.label}</span>
                </>
              )}
            </span>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] p-4">
        {daemonPresent && needsToken && (
          <TokenPrompt onSubmit={(value) => { setToken(value); void refreshDaemon(); }} />
        )}

        {screen === "Collect" &&
          (daemon ? (
            <Collect
              status={daemon}
              onBundle={(bundle) => {
                addBundles([bundle], { navigate: false });
                void refreshDaemon();
              }}
            />
          ) : (
            <Empty>The daemon is no longer reachable.</Empty>
          ))}

        {screen === "Runs" && (
          <Runs
            bundles={bundles}
            selected={selectedKey}
            onSelect={(key) => {
              setSelectedKey(key);
              setScreen("Overview");
            }}
            onAdd={addBundles}
            onRemove={removeBundle}
          />
        )}
        {screen === "Diff" && (
          <Diff
            bundles={bundles}
            aKey={baselineKey}
            bKey={selectedKey}
            onPick={(side, key) =>
              side === "a" ? setBaselineKey(key || null) : setSelectedKey(key || null)
            }
            onSwap={swapSides}
          />
        )}
        {screen !== "Runs" &&
          screen !== "Diff" &&
          screen !== "Collect" &&
          (selected ? (
            <>
              {screen === "Overview" && <Overview bundle={selected} />}
              {screen === "Flame" && <Flame bundle={selected} />}
              {screen === "Threads" && (
                <Threads bundle={selected} baseline={baseline} />
              )}
              {screen === "Locks" && <Locks bundle={selected} />}
              {screen === "Wakeups" && <Wakeups bundle={selected} />}
            </>
          ) : (
            <Empty>Select a run first.</Empty>
          ))}
      </main>
    </div>
  );
}

/**
 * Ask for the token when the daemon is there but this tab has not been given
 * one — a bookmarked URL, or a reload after the session storage was cleared.
 *
 * The daemon prints a link with the token in the query string; the page takes
 * it out of the URL on load precisely so that link is single-use in practice,
 * which is what makes this prompt necessary rather than a nuisance.
 */
function TokenPrompt({ onSubmit }: { onSubmit: (token: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (value.trim()) onSubmit(value.trim());
      }}
      className="mb-4 rounded border border-sky-500/50 bg-sky-500/10 px-3 py-2 text-sm text-sky-100"
    >
      <p className="mb-2">
        A Performer daemon is serving this page, but this tab has no token.
        Paste the one it printed at startup to enable measurements — or carry
        on reading bundles without it.
      </p>
      <div className="flex gap-2">
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          type="password"
          aria-label="daemon token"
          placeholder="token"
          className="flex-1 rounded border border-slate-600 bg-slate-950 px-2 py-1 font-mono text-sm text-slate-100"
        />
        <button
          type="submit"
          className="rounded bg-sky-600 px-3 py-1 text-sm text-white hover:bg-sky-500"
        >
          use it
        </button>
      </div>
    </form>
  );
}
