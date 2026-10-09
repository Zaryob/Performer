import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const root = fileURLToPath(new URL(".", import.meta.url));

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : entry.isFile() ? [path] : [];
  });
}

// A content fingerprint works for local builds and Docker builds alike; the
// Docker context intentionally excludes .git and a previously built dist/.
const revision = createHash("sha256");
for (const path of [
  ...sourceFiles(join(root, "src")),
  join(root, "index.html"),
  join(root, "package-lock.json"),
  join(root, "vite.config.ts"),
].sort()) {
  revision.update(relative(root, path));
  revision.update("\0");
  revision.update(readFileSync(path));
}
const buildRevision = revision.digest("hex").slice(0, 12);
const viewerVersion = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version as string;

// The viewer has to open from file://, where a browser refuses to load ES
// modules and forbids fetch() outright. Everything therefore has to end up
// inside one index.html: no separate chunks, no asset requests, no CSS link.
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  define: { __VIEWER_BUILD_REVISION__: JSON.stringify(buildRevision), __VIEWER_VERSION__: JSON.stringify(viewerVersion) },
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    reportCompressedSize: false,
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
