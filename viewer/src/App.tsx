import { useMemo, useState } from "react";
import type { Bundle } from "./bundle/load";
import { Runs } from "./screens/Runs";
import { Overview } from "./screens/Overview";
import { Flame } from "./screens/Flame";
import { Threads } from "./screens/Threads";
import { Empty } from "./components/ui";

const SCREENS = ["Runs", "Overview", "Flame", "Threads"] as const;
type Screen = (typeof SCREENS)[number];

/** Screens still to come, shown so their absence is a plan rather than a gap. */
const PLANNED: { name: string; milestone: string }[] = [
  { name: "Diff", milestone: "M4" },
  { name: "Locks", milestone: "M5" },
  { name: "Wakeups", milestone: "M5" },
  { name: "Timeline", milestone: "M5" },
];

export function App() {
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [screen, setScreen] = useState<Screen>("Runs");

  const selected = useMemo(
    () => bundles.find((bundle) => bundle.key === selectedKey) ?? null,
    [bundles, selectedKey],
  );

  const addBundles = (loaded: Bundle[]) => {
    setBundles((current) => [...current, ...loaded]);
    const first = loaded[0];
    if (first && !selectedKey) {
      setSelectedKey(first.key);
      setScreen("Overview");
    }
  };

  const removeBundle = (key: string) => {
    setBundles((current) => current.filter((bundle) => bundle.key !== key));
    if (selectedKey === key) setSelectedKey(null);
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
                disabled={name !== "Runs" && !selected}
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
        {screen !== "Runs" &&
          (selected ? (
            <>
              {screen === "Overview" && <Overview bundle={selected} />}
              {screen === "Flame" && <Flame bundle={selected} />}
              {screen === "Threads" && <Threads bundle={selected} />}
            </>
          ) : (
            <Empty>Select a run first.</Empty>
          ))}
      </main>
    </div>
  );
}
