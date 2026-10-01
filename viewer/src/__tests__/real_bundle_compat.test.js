import { existsSync, readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { loadBundleFile } from "../bundle/load";

// These captures are local field-test artifacts, not committed test fixtures.
// Keep the compatibility check runnable where they exist without making CI
// depend on a particular Pi or capture session.
const directory = fileURLToPath(new URL("../../../runs/pi5-live/real/", import.meta.url));
const captures = existsSync(directory)
  ? readdirSync(directory).filter((name) => name.endsWith("-v2.tgz")).sort()
  : [];

describe("actual Pi bundle compatibility", () => {
  it.skipIf(captures.length === 0)("opens every locally available v2 capture through File loading", async () => {
    for (const name of captures) {
      const file = new File([readFileSync(`${directory}/${name}`)], name, {
        type: "application/gzip",
      });
      const bundle = await loadBundleFile(file);
      expect(bundle.manifest.schema_version, name).toBe(2);
      expect(bundle.manifest.run_id, name).toBeTruthy();
      expect(bundle.files.has("manifest.json"), name).toBe(true);
      expect(bundle.system, name).not.toBeNull();
    }
  });
});
