/**
 * M3 and M4 acceptance checks.
 *
 * Opens the built `dist/index.html` over `file://` -- the way it will actually
 * be used, with no server anywhere -- loads real bundles through the file
 * picker, times how long the flame graph takes to appear, and then compares
 * two of them.
 *
 *   node verify.mjs <bundle.tgz> [more.tgz ...]
 *
 * With two or more bundles the M4 section runs: the first two are diffed and
 * the canvas is sampled to prove that paths which grew and paths which shrank
 * are actually drawn in distinguishable colours, which is what "the differing
 * code paths can be told apart" has to mean if it is to be checked at all.
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
// The last bundle was collected from a target without frame pointers. The
// viewer must refuse to let anyone argue from that graph, not merely draw it.
if (bundles.length > 2) {
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.locator("main table tbody tr").nth(bundles.length - 1).click();
  await page.getByRole("button", { name: "Flame", exact: true }).click();
  await page.waitForSelector("canvas");
  const warning = await page.locator("main").innerText();
  check(
    "unresolvable stacks are called out on the graph itself",
    warning.includes("could not be resolved") &&
      warning.includes("-fno-omit-frame-pointer"),
  );
  await page.screenshot({ path: "verify-broken.png" });

  // And a diff involving it must say so too: comparing unresolved stacks
  // against anything produces a confident picture of nothing.
  await page.getByRole("button", { name: "Diff", exact: true }).click();
  await page.waitForSelector("text=A — before");
  const brokenDiff = await page.locator("main").innerText();
  check(
    "a diff against an unusable run is flagged, not quietly drawn",
    /confident picture of nothing/.test(brokenDiff),
  );
}

// -- Diff: the M4 acceptance measurement ------------------------------------
if (bundles.length > 1) {
  console.log("\ncomparing the first two runs");
  await page.getByRole("button", { name: "Diff", exact: true }).click();
  await page.waitForSelector("text=A — before");

  // Pick the pair explicitly rather than relying on which one loaded first:
  // the check is about the comparison, not about the default selection.
  const options = await page
    .getByLabel("baseline run A")
    .locator("option")
    .evaluateAll((nodes) => nodes.map((n) => n.value).filter(Boolean));
  await page.getByLabel("baseline run A").selectOption(options[0]);
  await page.getByLabel("comparison run B").selectOption(options[1]);

  const diffStarted = Date.now();
  await page.waitForSelector("canvas", { timeout: 30_000 });
  await page.evaluate(
    () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
  );
  const diffMs = Date.now() - diffStarted;
  check("differential flame graph rendered in under 2 s", diffMs < 2000, `${diffMs} ms`);

  // The acceptance criterion is that the differing code paths can be told
  // apart, so read the pixels back and classify them. Red means the path grew
  // in B, blue that it shrank; a graph with only one of them, or with neither,
  // has not distinguished anything.
  const shading = await page.evaluate(() => {
    const canvas = document.querySelector("canvas");
    const ctx = canvas.getContext("2d");
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let grew = 0;
    let shrank = 0;
    let flat = 0;
    for (let i = 0; i < data.length; i += 4 * 13) {
      const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]];
      if (a === 0) continue;
      if (r - b > 30) grew += 1;
      else if (b - r > 30) shrank += 1;
      else if (Math.abs(r - g) < 12 && Math.abs(g - b) < 12) flat += 1;
    }
    return { grew, shrank, flat };
  });
  check(
    "paths that grew and paths that shrank are drawn in different colours",
    shading.grew > 200 && shading.shrank > 200,
    `${shading.grew} red, ${shading.shrank} blue, ${shading.flat} unchanged sampled pixels`,
  );

  const diffText = await page.locator("main").innerText();
  check(
    "the comparison says it normalised the two runs",
    /shares, so the .* and .* runs are on the same scale/.test(diffText),
  );
  check(
    "call paths present in only one run are listed separately",
    /New in B/.test(diffText) && /Gone from B/.test(diffText),
  );

  const movers = await page
    .locator("main table tbody tr")
    .first()
    .innerText()
    .catch(() => "");
  check("the biggest movers table has rows", movers.length > 0, movers.split("\n")[0] ?? "");

  // The synthetic pair models a heavier load on the same process, and the
  // thing it was built to make findable is the shared timer mutex.
  await page.getByPlaceholder("e.g. __lll_lock_wait").fill("__lll_lock_wait");
  await page.waitForTimeout(400);
  const searched2 = await page.locator("main").innerText();
  const matched2 = Number(searched2.match(/matched ([\d.]+)%/)?.[1] ?? "0");
  check(
    "searching the differential graph still finds and quantifies a frame",
    matched2 > 0,
    `${matched2}% of the combined profile`,
  );
  await page.getByPlaceholder("e.g. __lll_lock_wait").fill("");

  await page.screenshot({ path: "verify-diff.png", fullPage: false });

  // "B only" is difffolded.pl's default layout and it hides anything that
  // vanished. The default here is A + B precisely so it does not, and both
  // have to actually work.
  await page.getByLabel("layout basis").selectOption("after");
  await page.waitForTimeout(300);
  const afterOnly = await page.evaluate(() => {
    const canvas = document.querySelector("canvas");
    const ctx = canvas.getContext("2d");
    const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    let filled = 0;
    for (let i = 3; i < data.length; i += 4 * 97) if (data[i] > 0) filled += 1;
    return filled;
  });
  check("the layout basis can be switched", afterOnly > 100, `${afterOnly} sampled pixels`);
  await page.getByLabel("layout basis").selectOption("both");

  // -- thread deltas --------------------------------------------------------
  await page.getByRole("button", { name: "Threads", exact: true }).click();
  await page.waitForSelector("text=Versus");
  const threadDiffText = await page.locator("main").innerText();
  check(
    "threads are compared by name, with the reason stated",
    /tids are not stable across runs/.test(threadDiffText),
  );
  check(
    // Case insensitive: the table headers are uppercased by CSS, and
    // innerText reports what is on screen rather than what is in the markup.
    "thread deltas are per second so run lengths do not matter",
    /CPU ms\/s/i.test(threadDiffText) && /runq ms\/s/i.test(threadDiffText),
  );
}

check("no console errors", consoleErrors.length === 0, consoleErrors.join(" | "));

await page.getByRole("button", { name: "Threads", exact: true }).click();
await page.waitForSelector("main table tbody tr");
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
