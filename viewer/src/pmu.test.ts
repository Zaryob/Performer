import { describe, expect, it } from "vitest";
import { ratio, runningShare } from "./pmu";
import type { PmuEvent } from "./bundle/types";

const event = (scaled: number, running: number): PmuEvent => ({
  raw: scaled, scaled, time_enabled_ns: 100, time_running_ns: running,
});

describe("PMU quality gates", () => {
  it("reports a ratio only when both counters ran long enough", () => {
    expect(ratio(event(200, 100), event(100, 100))).toBe(2);
    expect(ratio(event(200, 89), event(100, 100))).toBeNull();
    expect(ratio(event(200, 100), event(0, 100))).toBeNull();
  });

  it("preserves the kernel's time-running share", () => {
    expect(runningShare(event(10, 50))).toBe(0.5);
  });
});
