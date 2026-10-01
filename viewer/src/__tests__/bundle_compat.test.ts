import { describe, expect, it } from "vitest";
import { bundleFromArchive, BundleError } from "../bundle/load";

function archiveWithManifestVersion(version: number): Uint8Array {
  const block = 512;
  const encoder = new TextEncoder();
  const body = encoder.encode(JSON.stringify({ schema_version: version }));
  const archive = new Uint8Array(block * 4);
  archive.set(encoder.encode("run_compat/manifest.json"), 0);
  archive.set(encoder.encode(body.length.toString(8).padStart(11, "0") + "\0"), 124);
  archive[156] = "0".charCodeAt(0);
  archive.set(body, block);
  return archive;
}

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
});
