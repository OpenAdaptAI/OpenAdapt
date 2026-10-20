#!/usr/bin/env node
// Visual snapshot tests: shoot the demo page in fixed states and compare each
// shot with the committed baseline (test/visual/baseline/<platform>/).
//
//   npm run demo &            # serves demo/ on 127.0.0.1:4517
//   node test/visual/shots.mjs            # compare; exit 1 on a difference
//   node test/visual/shots.mjs --update   # rewrite the baselines
//
// Needs Playwright with Chromium (PLAYWRIGHT=/path/to/playwright/index.mjs,
// default: the `playwright` package). Baselines are per platform because
// font rendering differs; CI runs the jsdom snapshot tests instead.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const update = process.argv.includes("--update");
const base = process.env.DEMO_URL ?? "http://127.0.0.1:4517/";
const outDir = path.join(here, update ? `baseline/${process.platform}` : `out/${process.platform}`);
const baseDir = path.join(here, `baseline/${process.platform}`);
const { chromium } = await import(process.env.PLAYWRIGHT ? pathToFileURL(process.env.PLAYWRIGHT).href : "playwright");

export const SHOTS = [
  { id: "referral-start", query: "?cursor=0" },
  { id: "referral-iteration-2", query: "?cursor=60" },
  { id: "referral-stopped", query: "?cursor=end" },
  { id: "referral-stopped-light", query: "?cursor=end&theme=light" },
  { id: "referral-unfolded", query: "?cursor=end&unfold=1" },
  { id: "referral-stop-rules", query: "?cursor=end&view=stops" },
  { id: "records-map", query: "?case=records" },
  { id: "records-evidence", query: "?case=records&view=evidence" },
  { id: "records-phone", query: "?case=records", viewport: { width: 375, height: 812 } },
];

fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch();
const failures = [];
for (const shot of SHOTS) {
  const context = await browser.newContext({
    viewport: shot.viewport ?? { width: 1240, height: 900 },
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  await page.goto(base + shot.query, { waitUntil: "networkidle" });
  await page.addStyleTag({ content: "*{transition:none!important;animation:none!important;caret-color:transparent!important}" });
  await page.evaluate(() => document.fonts?.ready);
  const file = path.join(outDir, `${shot.id}.png`);
  await page.locator(".opg-root").screenshot({ path: file });
  await context.close();
  if (update) continue;
  const baseline = path.join(baseDir, `${shot.id}.png`);
  if (!fs.existsSync(baseline)) failures.push(`${shot.id}: no baseline`);
  else if (!fs.readFileSync(baseline).equals(fs.readFileSync(file))) failures.push(`${shot.id}: differs from ${path.relative(process.cwd(), baseline)}`);
}
await browser.close();
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(update ? `wrote ${SHOTS.length} baselines to ${path.relative(process.cwd(), outDir)}` : `${SHOTS.length} shots match`);
