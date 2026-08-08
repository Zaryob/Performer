/**
 * M6 acceptance check.
 *
 * Drives the "New measurement" flow in a real browser against a real daemon:
 * picks a live process out of the target list, starts a collection, watches
 * the job run, and confirms the finished bundle is opened in the viewer
 * without anybody touching a file.
 *
 *   node verify-daemon.mjs <url> <token>
 *
 * The daemon is expected to already be running; starting it here would hide
 * exactly the part an operator has to get right.
 */

import { chromium } from "playwright";

const [url, token] = process.argv.slice(2);
if (!url || !token) {
  console.error("usage: node verify-daemon.mjs <url> <token>");
  process.exit(2);
}

const failures = [];
function check(label, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail ? `  ${detail}` : ""}`);
  if (!ok) failures.push(label);
}

const browser = await chromium.launch({
  executablePath: process.env.PERFORMER_CHROMIUM ?? "/opt/pw-browsers/chromium",
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => consoleErrors.push(String(e)));

console.log(`opening ${url}`);
await page.goto(`${url}?token=${encodeURIComponent(token)}`);
await page.waitForSelector("text=Drop run bundles here");

// The button appears only once the page has asked the daemon what it can do,
// so this waits rather than sampling — a race here would be the test's, not
// the product's.
const collectButton = page.getByRole("button", { name: "+ New measurement" });
await collectButton.waitFor({ state: "visible", timeout: 15_000 });
check("the daemon serves the viewer with its collection controls", true);

// The token arrives in the query string and must not stay there: that URL
// ends up in history, in screenshots, and in pasted chat messages.
check(
  "the token is taken out of the address bar",
  !page.url().includes(token),
  page.url(),
);
check(
  "and kept for the session instead",
  (await page.evaluate(() => sessionStorage.getItem("performer.token"))) === token,
);

// -- the collection ---------------------------------------------------------
await collectButton.click();
await page.waitForSelector("text=Target process");

const targetRows = await page.locator("main table tbody tr").count();
check("live processes are offered as targets", targetRows > 0, `${targetRows} rows`);

await page.getByLabel("filter targets").fill("contention");
await page.waitForTimeout(300);
const matching = await page.locator("main table tbody tr").count();
check("the target can be found by name", matching > 0, `${matching} match`);
if (!matching) {
  console.error("no contention process running; start one first");
  await browser.close();
  process.exit(1);
}

const chosen = await page.locator("main table tbody tr").first().innerText();
console.log(`        target: ${chosen.replace(/\n/g, "  ")}`);
await page.locator("main table tbody tr").first().click();

// The label defaults from the process name, already sanitised into something
// that can be a directory.
const defaulted = await page.getByLabel("label").inputValue();
check("a usable label is filled in from the process name", /^[A-Za-z0-9._-]+$/.test(defaulted), defaulted);

// A label the daemon would refuse must be refused here first.
await page.getByLabel("label").fill("run; rm -rf /");
await page.waitForTimeout(200);
check(
  "a label with shell metacharacters is refused in the form",
  await page.locator("text=letters, digits, dot, dash and underscore").isVisible(),
);
check(
  "and the button is disabled while it is invalid",
  await page.getByRole("button", { name: /Measure|Choose a process/ }).isDisabled(),
);

await page.getByLabel("label").fill("m6-acceptance");
await page.getByLabel("profile").selectOption("light");
// The shortest run that still exercises the whole path. This harness is
// checking the flow, not the measurement — M1 and M2 checked that.
await page.getByLabel("duration").fill("15");
await page.getByLabel("duration").dispatchEvent("input");
await page.waitForTimeout(200);
check(
  "the duration control reflects the choice",
  /15 s|15\.0 s/.test(await page.locator("text=/duration —/").innerText()),
  await page.locator("text=/duration —/").innerText(),
);

const started = Date.now();
await page.getByRole("button", { name: /^Measure / }).click();
await page.waitForSelector("text=/Job [0-9a-f]+/", { timeout: 20_000 });
check("the job appears and streams its log", true);

// The whole run: preflight, collection, bundle written, bundle opened here.
await page.waitForSelector("text=Collected and opened", { timeout: 180_000 });
const elapsed = ((Date.now() - started) / 1000).toFixed(1);
check("the measurement completed", true, `${elapsed} s`);

const jobText = await page.locator("main").innerText();
const runId = (jobText.match(/\d{8}T\d{6}Z-m6-acceptance/) ?? [])[0];
check("the bundle has a run id", Boolean(runId), runId ?? "");

// -- the payoff -------------------------------------------------------------
// It is already loaded: no download folder, no file picker, no second tool.
await page.getByRole("button", { name: "Runs", exact: true }).click();
await page.waitForSelector("main table tbody tr");
const rows = await page.locator("main table tbody tr").count();
check("the finished run is already loaded in the viewer", rows >= 1, `${rows} row`);

await page.locator("main table tbody tr").first().click();
await page.waitForSelector("text=Verdict");
const overview = await page.locator("main").innerText();
check("it opens like any other bundle", /m6-acceptance/.test(overview));

await page.getByRole("button", { name: "Threads", exact: true }).click();
await page.waitForSelector("main table tbody tr");
const threadRows = await page.locator("main table tbody tr").count();
check("with real thread data from the target", threadRows > 50, `${threadRows} threads`);

await page.screenshot({ path: "verify-collect.png" });

check("no console errors", consoleErrors.length === 0, consoleErrors.join(" | "));

await browser.close();
console.log(failures.length ? `\nFAILED: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
