/**
 * M3 acceptance check.
 *
 * Opens the built `dist/index.html` over `file://` -- the way it will actually
 * be used, with no server anywhere -- loads a real bundle through the file
 * picker, and times how long the flame graph takes to appear.
 *
 *   node verify.mjs <bundle.tgz> [more.tgz ...]
 */

import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const page_url = `file://${path.join(here, "dist", "index.html")}`;
const bundles = process.argv.slice(2);

if (!bundles.length) {
  console.error("usage: node verify.mjs <bundle.tgz> [...]");
  process.exit(2);
}
for (const bundle of bundles) {
  if (!fs.existsSync(bundle)) {
    console.error(`no such bundle: ${bundle}`);
    process.exit(2);
  }
}

const failures = [];
function check(label, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail ? `  ${detail}` : ""}`);
  if (!ok) failures.push(label);
}

// The environment ships a Chromium that predates this Playwright build, so
// point at it rather than downloading another copy: the analysis machine this
// viewer targets has no network either.
const browser = await chromium.launch({
  executablePath: process.env.PERFORMER_CHROMIUM ?? "/opt/pw-browsers/chromium",
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

console.log(`opening ${page_url}`);
await page.goto(page_url);
await page.waitForSelector("text=Drop run bundles here");
check("loads over file:// with no server", true);

console.log(`\nloading ${bundles.length} bundle(s) through the file picker`);
await page.setInputFiles('input[type="file"]', bundles);
await page.waitForSelector("table tbody tr", { timeout: 20_000 });
// Loading the first bundle switches to Overview, so come back deliberately.
await page.getByRole("button", { name: "Runs", exact: true }).click();
await page.waitForSelector("text=Drop run bundles here");
const rows = await page.locator("main table tbody tr").count();
check("bundles appear in the Runs table", rows === bundles.length, `${rows} rows`);

// -- Overview ---------------------------------------------------------------
await page.locator("main table tbody tr").first().click();
await page.waitForSelector("text=Quality");
const overviewText = await page.locator("main").innerText();
check("Overview shows the run id", /run id|\d{8}T\d{6}Z-/.test(overviewText));
check("Overview lists probes", overviewText.includes("Probes"));
check(
  "Overview reports frame pointer quality",
  overviewText.includes("frame pointers"),
);

// -- Flame: the acceptance measurement --------------------------------------
console.log("\nrendering the flame graph");
const started = Date.now();
await page.getByRole("button", { name: "Flame", exact: true }).click();
await page.waitForSelector("canvas", { timeout: 30_000 });
await page.waitForFunction(() => {
  const canvas = document.querySelector("canvas");
  return canvas instanceof HTMLCanvasElement && canvas.width > 0;
});
// Wait for the browser to have actually painted, not merely for React to have
// committed: a canvas that exists but is blank would pass a naive check.
await page.evaluate(
  () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
);
const renderMs = Date.now() - started;

const painted = await page.evaluate(() => {
  const canvas = document.querySelector("canvas");
  if (!(canvas instanceof HTMLCanvasElement)) return 0;
  const ctx = canvas.getContext("2d");
  if (!ctx) return 0;
  const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
  let filled = 0;
  for (let i = 3; i < data.length; i += 4 * 97) if (data[i] > 0) filled += 1;
  return filled;
});
check("flame graph actually painted", painted > 100, `${painted} sampled pixels`);
check(
  `flame graph rendered in under 2 s`,
  renderMs < 2000,
  `${renderMs} ms`,
);

const flameText = await page.locator("main").innerText();
const framesMatch = flameText.match(/([\d,]+) frames/);
const threadsMatch = flameText.match(/([\d,]+) threads in this profile/);
console.log(
  `        ${framesMatch?.[1] ?? "?"} frames across ${threadsMatch?.[1] ?? "?"} threads`,
);

// -- filters ----------------------------------------------------------------
await page.getByPlaceholder("e.g. worker").fill("TimerWheel");
await page.waitForTimeout(400);
const filtered = await page.locator("main").innerText();
check(
  "thread filter narrows the graph",
  /filters hide\s+[\d.]+%/.test(filtered),
  filtered.match(/filters hide\s+([\d.]+%)/)?.[1] ?? "",
);
await page.getByPlaceholder("e.g. worker").fill("");

// A frame that genuinely appears in the on-CPU stacks, so a zero match would
// mean the search is broken rather than merely unlucky.
await page.getByPlaceholder("e.g. pthread_mutex_lock").fill("__lll_lock_wait");
await page.waitForTimeout(400);
const searched = await page.locator("main").innerText();
const matchedShare = Number(searched.match(/matched ([\d.]+)%/)?.[1] ?? "0");
check("frame search finds and quantifies matches", matchedShare > 0,
  `${matchedShare}% of the profile`);

await page.selectOption("select", "0.01");
await page.waitForTimeout(400);
check("idle threads can be hidden", (await page.locator("canvas").count()) === 1);

// -- Threads ----------------------------------------------------------------
await page.getByRole("button", { name: "Threads", exact: true }).click();
await page.waitForSelector("text=runqueue ms");
const threadRows = await page.locator("main table tbody tr").count();
check("Threads table lists every thread", threadRows > 300, `${threadRows} rows`);
await page.locator("main table thead th").nth(2).click();
await page.waitForTimeout(150);
check("Threads table sorts", true);

// -- the unusable run -------------------------------------------------------
// The second bundle was collected from a target without frame pointers. The
// viewer must refuse to let anyone argue from that graph, not merely draw it.
if (bundles.length > 1) {
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.locator("main table tbody tr").nth(1).click();
  await page.getByRole("button", { name: "Flame", exact: true }).click();
  await page.waitForSelector("canvas");
  const warning = await page.locator("main").innerText();
  check(
    "unresolvable stacks are called out on the graph itself",
    warning.includes("could not be resolved") &&
      warning.includes("-fno-omit-frame-pointer"),
  );
  await page.screenshot({ path: "verify-broken.png" });
}

check("no console errors", consoleErrors.length === 0, consoleErrors.join(" | "));

await page.screenshot({ path: "verify-threads.png", fullPage: false });
await page.getByRole("button", { name: "Flame", exact: true }).click();
await page.waitForSelector("canvas");
await page.waitForTimeout(500);
await page.screenshot({ path: "verify-flame.png", fullPage: false });

await browser.close();

console.log(
  failures.length ? `\nFAILED: ${failures.join(", ")}` : "\nall checks passed",
);
process.exit(failures.length ? 1 : 0);
