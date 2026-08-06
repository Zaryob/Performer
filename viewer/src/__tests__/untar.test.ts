import { describe, expect, it } from "vitest";
import { untar, TarError } from "../bundle/untar";

const BLOCK = 512;

/** Build a tar header block by hand, so the tests can produce hostile input. */
function header(name: string, size: number, typeFlag = "0"): Uint8Array {
  const block = new Uint8Array(BLOCK);
  const encoder = new TextEncoder();
  block.set(encoder.encode(name.slice(0, 100)), 0);
  block.set(encoder.encode("0000644\0"), 100);
  block.set(encoder.encode("0000000\0"), 108);
  block.set(encoder.encode("0000000\0"), 116);
  block.set(encoder.encode(size.toString(8).padStart(11, "0") + "\0"), 124);
  block.set(encoder.encode("00000000000\0"), 136);
  block.set(encoder.encode(typeFlag), 156);
  block.set(encoder.encode("ustar\0"), 257);
  block.set(encoder.encode("00"), 263);
  // Checksum: spaces during computation, then the octal value. untar does not
  // verify it, but a well formed archive carries one.
  block.fill(32, 148, 156);
  let sum = 0;
  for (const byte of block) sum += byte;
  block.set(encoder.encode(sum.toString(8).padStart(6, "0") + "\0 "), 148);
  return block;
}

function archive(members: { name: string; body?: string; type?: string }[]): Uint8Array {
  const parts: Uint8Array[] = [];
  const encoder = new TextEncoder();
  for (const member of members) {
    const body = encoder.encode(member.body ?? "");
    parts.push(header(member.name, body.length, member.type ?? "0"));
    if (body.length) {
      const padded = new Uint8Array(Math.ceil(body.length / BLOCK) * BLOCK);
      padded.set(body);
      parts.push(padded);
    }
  }
  parts.push(new Uint8Array(BLOCK * 2)); // end of archive
  const total = parts.reduce((n, part) => n + part.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}

const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);

describe("untar", () => {
  it("strips the single top level run directory", () => {
    const entries = untar(
      archive([
        { name: "run_x/", type: "5" },
        { name: "run_x/manifest.json", body: "{}" },
        { name: "run_x/stacks/oncpu.folded", body: "a;b 1\n" },
      ]),
    );
    expect(entries.map((e) => e.path)).toEqual([
      "manifest.json",
      "stacks/oncpu.folded",
    ]);
    expect(decode(entries[0]!.bytes)).toBe("{}");
  });

  it("skips PAX headers, which python's tarfile writes by default", () => {
    // Without this every bundle the collector produces would be rejected over
    // a member name that was never meant to be a path.
    const entries = untar(
      archive([
        { name: "././@PaxHeader", body: "30 mtime=1754476332.6\n", type: "x" },
        { name: "run_x/manifest.json", body: "{}" },
      ]),
    );
    expect(entries.map((e) => e.path)).toEqual(["manifest.json"]);
  });

  it("honours a PAX path override", () => {
    const long = `run_x/${"deep/".repeat(30)}file.json`;
    const record = `path=${long}\n`;
    const body = `${record.length + 4} ${record}`;
    const entries = untar(
      archive([
        { name: "././@PaxHeader", body, type: "x" },
        { name: "run_x/truncated", body: "{}" },
      ]),
    );
    expect(entries[0]!.path).toBe(long.slice("run_x/".length));
  });

  it("reads GNU long names", () => {
    const long = `run_x/${"a".repeat(120)}.json`;
    const entries = untar(
      archive([
        { name: "././@LongLink", body: `${long}\0`, type: "L" },
        { name: "run_x/ignored", body: "{}" },
      ]),
    );
    expect(entries[0]!.path).toBe(`${"a".repeat(120)}.json`);
  });

  it("rejects a link member", () => {
    expect(() =>
      untar(archive([{ name: "run_x/evil", type: "2" }])),
    ).toThrowError(/link member/);
  });

  it("rejects a device member", () => {
    expect(() =>
      untar(archive([{ name: "run_x/dev", type: "3" }])),
    ).toThrowError(/device or fifo/);
  });

  it("rejects a traversal path", () => {
    expect(() =>
      untar(archive([{ name: "run_x/../../etc/passwd", body: "x" }])),
    ).toThrowError(/unsafe member path/);
  });

  it("rejects an archive with more than one top level directory", () => {
    expect(() =>
      untar(
        archive([
          { name: "run_a/manifest.json", body: "{}" },
          { name: "run_b/manifest.json", body: "{}" },
        ]),
      ),
    ).toThrowError(/one top level run directory/);
  });

  it("rejects an empty archive", () => {
    expect(() => untar(archive([]))).toThrowError(TarError);
  });

  it("stops at the end of archive marker", () => {
    const data = archive([{ name: "run_x/a", body: "1" }]);
    const padded = new Uint8Array(data.length + BLOCK * 4);
    padded.set(data);
    expect(untar(padded)).toHaveLength(1);
  });
});
