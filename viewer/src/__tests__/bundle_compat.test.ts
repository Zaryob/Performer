import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { bundleFromArchive, BundleError } from "../bundle/load";
import { Runs } from "../screens/Runs";
import { Overview } from "../screens/Overview";

function archiveWithManifest(manifest: Record<string, unknown>): Uint8Array {
  const block = 512;
  const encoder = new TextEncoder();
  const body = encoder.encode(JSON.stringify(manifest));
  const archive = new Uint8Array(block * (Math.ceil(body.length / block) + 3));
  archive.set(encoder.encode("run_compat/manifest.json"), 0);
  archive.set(encoder.encode(body.length.toString(8).padStart(11, "0") + "\0"), 124);
  archive[156] = "0".charCodeAt(0);
  archive.set(body, block);
  return archive;
}

const archiveWithManifestVersion = (version: number) =>
  archiveWithManifest({ schema_version: version });

describe("bundle format compatibility", () => {
  it("opens the old and current bundle versions", () => {
    for (const version of [1, 2]) {
      const bundle = bundleFromArchive("compat.tar", archiveWithManifestVersion(version));
      expect(bundle.manifest.schema_version).toBe(version);
    }
  });

  it("refuses a future bundle version with a clear error", () => {
    expect(() => bundleFromArchive("future.tar", archiveWithManifestVersion(3))).toThrowError(
      BundleError,
    );
  });

  it("loads and lists a complete v2 bundle with an unavailable overhead estimate", () => {
    const manifest = {
      schema_version: 2,
      run_id: "20261001T130337Z-compat",
      label: "compat",
      started_at: "2026-10-01T13:03:37Z",
      duration_s: 30,
      profile: "standard",
      status: "ok",
      target: { pid: 1016, comm: "rrdcached", thread_count_start: 12, thread_count_end: 12 },
      probes: [],
      tool_versions: {},
      quality: {
        frame_pointers_ok: true,
        unknown_frame_ratio: 0,
        estimated_overhead_pct: null,
        notes: ["overhead could not be estimated: CPU baseline or samples were unavailable"],
      },
    };
    const bundle = bundleFromArchive("compat.tar", archiveWithManifest(manifest));
    const html = renderToStaticMarkup(createElement(Runs, {
      bundles: [bundle],
      selected: null,
      onSelect: () => {},
      onAdd: () => {},
      onRemove: () => {},
    }));
    expect(html).toContain("compat");
    expect(html).toContain("n/a");
    expect(renderToStaticMarkup(createElement(Overview, { bundle }))).toContain("n/a");
  });
});
