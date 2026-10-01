/**
 * File -> Bundle.
 *
 * Everything happens in the browser. There is no fetch anywhere in the viewer
 * -- not as an optimisation but because `file://` forbids it, and the whole
 * point of the static viewer is that `dist/index.html` opens by double click
 * on a machine with no server and no toolchain.
 */

import { inflate } from "pako";
import { untar, TarError } from "./untar";
import {
  SUPPORTED_SCHEMA_VERSION,
  MIN_SUPPORTED_SCHEMA_VERSION,
  type Manifest,
  type SystemDoc,
  type ThreadsDoc,
  PATHS,
} from "./types";

export class BundleError extends Error {}

export interface Bundle {
  /** Stable id for React keys and selection; unique per load. */
  key: string;
  fileName: string;
  manifest: Manifest;
  system: SystemDoc | null;
  threads: ThreadsDoc | null;
  /** Bundle relative path -> raw bytes. */
  files: Map<string, Uint8Array>;
  loadedAt: number;
}

const GZIP_MAGIC = [0x1f, 0x8b];

function isGzip(data: Uint8Array): boolean {
  return data[0] === GZIP_MAGIC[0] && data[1] === GZIP_MAGIC[1];
}

function decodeText(bytes: Uint8Array): string {
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

function parseJson<T>(bytes: Uint8Array, path: string): T {
  try {
    return JSON.parse(decodeText(bytes)) as T;
  } catch (error) {
    throw new BundleError(`${path} is not valid JSON: ${(error as Error).message}`);
  }
}

/** Read a `.tgz` (or an already uncompressed `.tar`) into its members. */
export function readArchive(data: Uint8Array): Map<string, Uint8Array> {
  let tar = data;
  if (isGzip(data)) {
    try {
      tar = inflate(data);
    } catch (error) {
      throw new BundleError(`could not decompress: ${(error as Error).message}`);
    }
  }
  try {
    return new Map(untar(tar).map((entry) => [entry.path, entry.bytes]));
  } catch (error) {
    if (error instanceof TarError) throw new BundleError(error.message);
    throw error;
  }
}

let counter = 0;

export function bundleFromArchive(fileName: string, data: Uint8Array): Bundle {
  const files = readArchive(data);
  const manifestBytes = files.get(PATHS.manifest);
  if (!manifestBytes) {
    throw new BundleError(`no ${PATHS.manifest} in ${fileName}; is this a run bundle?`);
  }
  const manifest = parseJson<Manifest>(manifestBytes, PATHS.manifest);

  // A bundle from a future collector is exactly the case where guessing
  // produces a confident wrong answer, so it is refused rather than
  // half-read.
  if (
    !Number.isInteger(manifest.schema_version) ||
    manifest.schema_version < MIN_SUPPORTED_SCHEMA_VERSION ||
    manifest.schema_version > SUPPORTED_SCHEMA_VERSION
  ) {
    throw new BundleError(
      `${fileName} uses bundle format version ${manifest.schema_version}; ` +
        `this viewer reads versions ${MIN_SUPPORTED_SCHEMA_VERSION}–${SUPPORTED_SCHEMA_VERSION}. Use a matching viewer.`,
    );
  }

  const systemBytes = files.get(PATHS.system);
  const threadsBytes = files.get(PATHS.threads);
  counter += 1;
  return {
    key: `${manifest.run_id}#${counter}`,
    fileName,
    manifest,
    system: systemBytes ? parseJson<SystemDoc>(systemBytes, PATHS.system) : null,
    threads: threadsBytes ? parseJson<ThreadsDoc>(threadsBytes, PATHS.threads) : null,
    files,
    loadedAt: Date.now(),
  };
}

export async function loadBundleFile(file: File): Promise<Bundle> {
  const buffer = await file.arrayBuffer();
  return bundleFromArchive(file.name, new Uint8Array(buffer));
}

// -- accessors --------------------------------------------------------------

export function hasFile(bundle: Bundle, path: string): boolean {
  return bundle.files.has(path);
}

export function readText(bundle: Bundle, path: string): string | null {
  const bytes = bundle.files.get(path);
  return bytes ? decodeText(bytes) : null;
}

export function readJson<T>(bundle: Bundle, path: string): T | null {
  const bytes = bundle.files.get(path);
  return bytes ? parseJson<T>(bytes, path) : null;
}

/** Parse a `series/*.csv` into row objects, keyed by the header. */
export function readSeries(
  bundle: Bundle,
  path: string,
): Record<string, number>[] {
  const text = readText(bundle, path);
  if (!text) return [];
  const lines = text.split("\n").filter((line) => line.trim() !== "");
  if (lines.length < 2) return [];
  const header = (lines[0] as string).split(",");
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row: Record<string, number> = {};
    header.forEach((name, index) => {
      row[name] = Number(cells[index] ?? 0);
    });
    return row;
  });
}

export function runDuration(manifest: Manifest): number {
  return manifest.actual_duration_s ?? manifest.duration_s;
}
