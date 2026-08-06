/**
 * A ustar reader.
 *
 * Small enough not to be worth a dependency, and writing it means the same
 * defences the collector applies can be applied here: a bundle is a file that
 * arrived from another machine, and the viewer opens it without asking anyone.
 * Nothing is ever written to disk, so path traversal cannot escape anywhere --
 * but a member claiming to be `../../etc/passwd` still means the archive is
 * not what it says it is, and displaying its contents as if it were run data
 * would be wrong.
 */

const BLOCK = 512;

/** Refuse a single member larger than this; the collector caps at the same size. */
const MAX_MEMBER_BYTES = 512 * 1024 * 1024;

export interface TarEntry {
  /** Path with the single top level run directory stripped off. */
  path: string;
  bytes: Uint8Array;
}

export class TarError extends Error {}

function readString(block: Uint8Array, offset: number, length: number): string {
  let end = offset;
  const limit = offset + length;
  while (end < limit && block[end] !== 0) end += 1;
  return new TextDecoder().decode(block.subarray(offset, end));
}

function readOctal(block: Uint8Array, offset: number, length: number): number {
  const text = readString(block, offset, length).trim();
  if (!text) return 0;
  const value = parseInt(text, 8);
  if (!Number.isFinite(value)) throw new TarError(`bad numeric field: ${text}`);
  return value;
}

function isZeroBlock(block: Uint8Array): boolean {
  for (let i = 0; i < block.length; i += 1) if (block[i] !== 0) return false;
  return true;
}

/**
 * Read every regular file out of a tar archive.
 *
 * Returns the entries with the archive's single top level directory removed,
 * so callers address members by their bundle relative path.
 */
export function untar(data: Uint8Array): TarEntry[] {
  const entries: TarEntry[] = [];
  const tops = new Set<string>();
  let offset = 0;
  let longName: string | null = null;
  let paxName: string | null = null;

  while (offset + BLOCK <= data.length) {
    const header = data.subarray(offset, offset + BLOCK);
    if (isZeroBlock(header)) break; // end of archive marker

    const rawName = longName ?? readString(header, 0, 100);
    const prefix = readString(header, 345, 155);
    const size = readOctal(header, 124, 12);
    const typeFlag = String.fromCharCode(header[156] ?? 0) || "0";
    longName = null;
    offset += BLOCK;

    if (size > MAX_MEMBER_BYTES) {
      throw new TarError(`member ${rawName} is implausibly large`);
    }
    const dataStart = offset;
    offset += Math.ceil(size / BLOCK) * BLOCK;

    // GNU long name: the next header's name lives in this member's payload.
    if (typeFlag === "L") {
      longName = new TextDecoder()
        .decode(data.subarray(dataStart, dataStart + size))
        .replace(/\0+$/, "");
      continue;
    }
    if (typeFlag === "K") continue; // long link name; links are rejected below

    // PAX extended headers. Python's tarfile writes these by default, so
    // every bundle the collector produces has them: they are metadata for the
    // *next* entry, carried in a pseudo-member named `././@PaxHeader`, and
    // treating one as a file would reject the archive over a path that was
    // never meant to be a path.
    if (typeFlag === "x" || typeFlag === "g") {
      const records = parsePax(data.subarray(dataStart, dataStart + size));
      const override = records.get("path");
      if (typeFlag === "x" && override) paxName = override;
      continue;
    }

    let name = paxName ?? (prefix ? `${prefix}/${rawName}` : rawName);
    paxName = null;
    if (name.startsWith("./")) name = name.slice(2);
    if (!name || name === ".") continue;

    if (typeFlag === "1" || typeFlag === "2") {
      throw new TarError(
        `archive contains a link member (${name}); bundles are plain files only`,
      );
    }
    if (typeFlag === "3" || typeFlag === "4" || typeFlag === "6") {
      throw new TarError(`archive contains a device or fifo member (${name})`);
    }

    const parts = name.split("/");
    if (name.startsWith("/") || parts.some((p) => p === "" || p === "." || p === "..")) {
      // Only reachable for names with a traversal component: a trailing slash
      // on a directory produces an empty last part, so those are filtered
      // before this check by the directory branch below.
      if (!(typeFlag === "5" && parts[parts.length - 1] === "")) {
        throw new TarError(`archive contains an unsafe member path: ${name}`);
      }
    }
    tops.add(parts[0] as string);

    if (typeFlag === "5") continue; // directory
    if (typeFlag !== "0" && typeFlag !== "\0" && typeFlag !== "7") {
      throw new TarError(`unsupported member type '${typeFlag}' in ${name}`);
    }

    entries.push({
      path: parts.slice(1).join("/"),
      bytes: data.subarray(dataStart, dataStart + size),
    });
  }

  if (tops.size !== 1) {
    throw new TarError(
      `expected exactly one top level run directory, found ${
        tops.size === 0 ? "none" : [...tops].sort().join(", ")
      }`,
    );
  }
  return entries.filter((entry) => entry.path !== "");
}

/**
 * Parse PAX extended header records.
 *
 * Each is `<length> <key>=<value>\n`, where the length counts the whole
 * record including its own digits.
 */
function parsePax(payload: Uint8Array): Map<string, string> {
  const records = new Map<string, string>();
  const text = new TextDecoder().decode(payload);
  let index = 0;
  while (index < text.length) {
    const space = text.indexOf(" ", index);
    if (space < 0) break;
    const length = Number(text.slice(index, space));
    if (!Number.isFinite(length) || length <= 0) break;
    const record = text.slice(space + 1, index + length).replace(/\n$/, "");
    const equals = record.indexOf("=");
    if (equals > 0) {
      records.set(record.slice(0, equals), record.slice(equals + 1));
    }
    index += length;
  }
  return records;
}
