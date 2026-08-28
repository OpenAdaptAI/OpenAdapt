#!/usr/bin/env node
// Reproduces every published Desktop screenshot.
//
// Before this existed, the cockpit images came from a fixture adapter that
// lived only in one branch, so re-shooting them after a UI or palette change
// meant rebuilding the adapter from scratch. They then sat on the marketing
// site through a palette change that the app itself never received.
//
// scripts/cockpit-targets.mjs holds the whole published set and names each
// target's consumer, because two repositories publish from it:
//
//   openadapt-web  public/desktop-preview/cockpit/, pinned by name, hash and
//                  geometry in public/desktop-preview/MANIFEST.json.
//   openadapt-ops  docs/assets/screenshots/program-workbench-desktop.png on
//                  docs.openadapt.ai, pinned by hash and measured palette in
//                  docs/assets/visual-palette.json. That image had no
//                  generator at all before it became a target here.
//
// scripts/cockpitTargets.test.ts fails if either list stops agreeing with the
// targets — a partial re-shoot that leaves some states on the old palette is
// exactly how the last set went stale.
//
// Usage:
//   npm run dev                                          # in one shell
//   node scripts/capture-cockpit-previews.mjs --out ./captures
//
// `npm run dev` and not a preview build: the program-workbench route is gated
// on import.meta.env.DEV in src/main.tsx, so a production build cannot reach it.
//
// Playwright is not a dependency of this app: it runs by hand after a UI
// change, and adding a browser download to every `npm ci` would be a poor
// trade. Install it anywhere and point PLAYWRIGHT_MODULE at it, or rely on the
// npx cache.
//
// Every capture is synthetic. scripts/cockpit-fixtures.mjs is invented data.
// No Tauri window, engine sidecar, account, Cloud session, recording, physical
// input, customer data, or live execution is involved.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { ipcTable } from './cockpit-fixtures.mjs';
import { TARGETS, VIEWPORT, viewportFor } from './cockpit-targets.mjs';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const argValue = (flag, fallback) => {
  const index = args.indexOf(flag);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const base = argValue('--base', 'http://127.0.0.1:1420').replace(/\/$/, '');
const outDir = path.resolve(argValue('--out', path.join(repo, 'captures')));
const only = argValue('--only', '');

// The harness stands in for the Tauri host. `invoke` is the only thing the
// frontend uses to reach the engine (src/lib/engine.ts), so answering it from a
// table is the whole adapter. Events are never emitted: a screenshot is a
// still, and a live event stream would make two runs of this script disagree.
function installTauriHost(table, localSessionKey, localSession) {
  const callbacks = new Map();
  let nextId = 1;

  try {
    if (localSession) window.localStorage.setItem(localSessionKey, 'enabled');
    else window.localStorage.removeItem(localSessionKey);
  } catch {
    /* a storage-restricted context still renders */
  }

  window.__TAURI_INTERNALS__ = {
    transformCallback(callback, once) {
      const id = nextId++;
      callbacks.set(id, { callback, once });
      return id;
    },
    unregisterCallback(id) {
      callbacks.delete(id);
    },
    convertFileSrc(filePath) {
      return filePath;
    },
    async invoke(cmd, args) {
      if (cmd === 'plugin:event|listen') return nextId++;
      if (cmd === 'plugin:event|unlisten') return undefined;
      if (cmd === 'engine_invoke') {
        const name = args?.cmd;
        const value = table[name];
        if (value === undefined) throw new Error(`no fixture for engine command ${name}`);
        // A per-workflow table is keyed by workflow_id; anything else answers flat.
        const workflowId = args?.params?.workflow_id;
        if (workflowId && value && typeof value === 'object' && !Array.isArray(value)
            && Object.prototype.hasOwnProperty.call(value, workflowId)) {
          return value[workflowId];
        }
        return value;
      }
      if (Object.prototype.hasOwnProperty.call(table, cmd)) return table[cmd];
      // Overlay and window commands are host-side and have no bearing on a
      // screenshot of the cockpit.
      return undefined;
    },
  };
}

const { chromium } = await (async () => {
  const override = process.env.PLAYWRIGHT_MODULE;
  const candidates = override
    ? [override, path.join(override, 'index.mjs'), path.join(override, 'index.js')]
    : ['playwright'];
  const failures = [];
  for (const candidate of candidates) {
    try {
      return await import(candidate.startsWith('.') || candidate.startsWith('/')
        ? `file://${path.resolve(candidate)}`
        : candidate);
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(
    'Playwright is not installed. Run `npm i playwright && npx playwright install chromium` '
      + `somewhere and pass PLAYWRIGHT_MODULE=/that/node_modules/playwright.\n  ${failures.join('\n  ')}`,
  );
})();

fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch();
const results = [];

async function settle(page) {
  await page.waitForLoadState('load');
  await page.waitForLoadState('networkidle', { timeout: 5_000 }).catch(() => {});
  await page.evaluate(() => document.fonts?.ready).catch(() => {});
  // Freeze anything that would make two runs of this script disagree. Smooth
  // scrolling is in that set: the in-app jump links animate, so a screenshot
  // taken straight after a jump lands at an arbitrary scroll offset.
  await page.addStyleTag({
    content: '*, *::before, *::after { animation: none !important;'
      + ' transition: none !important; caret-color: transparent !important;'
      + ' scroll-behavior: auto !important; }',
  });
  await page.waitForTimeout(500);
}

for (const target of TARGETS) {
  if (only && !target.file.includes(only)) continue;
  console.log(`\n${target.file}  <-  ${target.surface}`);

  const viewport = viewportFor(target);
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    reducedMotion: 'reduce',
    colorScheme: 'light',
  });
  // A surface that renders from static data inside the app needs no fixture
  // host, and installing one would claim provenance the image does not have.
  if (target.session) {
    await context.addInitScript(
      ({ table: t, key, localSession, source }) => {
        // eslint-disable-next-line no-new-func
        new Function('table', 'localSessionKey', 'localSession', `(${source})(table, localSessionKey, localSession)`)(
          t, key, localSession,
        );
      },
      {
        table: ipcTable(target.session),
        key: 'openadapt.desktop.local-session.v1',
        localSession: target.session.localSession,
        source: installTauriHost.toString(),
      },
    );
  }

  const page = await context.newPage();
  page.on('pageerror', (error) => console.warn(`  ! page error: ${error.message}`));
  await page.goto(`${base}/${target.query || ''}`, { waitUntil: 'domcontentloaded', timeout: 120_000 });
  await settle(page);

  if (target.nav) {
    await page.getByRole('button', { name: target.nav, exact: true }).click();
    await page.waitForTimeout(500);
  }

  if (target.openRow) {
    const workflow = target.session.workflows.find((w) => w.id === target.openRow.workflow);
    if (!workflow) throw new Error(`no fixture workflow ${target.openRow.workflow}`);
    const row = page.getByRole('row').filter({ hasText: workflow.name });
    await row.getByRole('button', { name: target.openRow.action, exact: true }).click();
    await page.waitForTimeout(900);
  }

  // The qualification screen is longer than one viewport. Its journey steps are
  // the in-app jump links an operator uses, so the shot frames the section the
  // same way the application does.
  if (target.journey) {
    await page.getByRole('button', { name: `Open ${target.journey}`, exact: true }).click();
    await page.waitForTimeout(500);
  }

  await settle(page);

  const absolute = path.join(outDir, target.file);
  await page.screenshot({ path: absolute, type: 'png' });
  const bytes = fs.readFileSync(absolute);
  const entry = {
    file: target.file,
    consumer: target.consumer,
    surface: target.surface,
    shows: target.shows,
    bytes: bytes.byteLength,
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    width: viewport.width,
    height: viewport.height,
  };
  results.push(entry);
  console.log(`  ${entry.bytes} bytes  ${entry.sha256}`);
  await context.close();
}

await browser.close();

fs.writeFileSync(
  path.join(outDir, 'captures.json'),
  `${JSON.stringify({
    base,
    captured_at: new Date().toISOString(),
    synthetic_fixture: true,
    // The cockpit geometry. A target that overrides it records its own width
    // and height in its result below.
    viewport: VIEWPORT,
    results,
  }, null, 2)}\n`,
);
console.log(`\n${results.length} captures written to ${outDir}`);
