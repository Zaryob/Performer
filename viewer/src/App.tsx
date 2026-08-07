import { useMemo, useState } from "react";
import type { Bundle } from "./bundle/load";
import { Runs } from "./screens/Runs";
import { Overview } from "./screens/Overview";
import { Flame } from "./screens/Flame";
import { Threads } from "./screens/Threads";
import { Diff } from "./screens/Diff";
import { Locks } from "./screens/Locks";
import { Wakeups } from "./screens/Wakeups";
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
type Screen = (typeof SCREENS)[number];

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

  const addBundles = (loaded: Bundle[]) => {
    if (!loaded.length) return;
    setBundles((current) => [...current, ...loaded]);
    const newest = loaded[loaded.length - 1] as Bundle;
    // A run is loaded alongside another one in order to compare it, so pair
    // them up rather than making the operator choose twice. Reading order is
    // the useful convention: what arrived first is "before".
    if (!selectedKey) {
      setSelectedKey(newest.key);
      setScreen("Overview");
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
