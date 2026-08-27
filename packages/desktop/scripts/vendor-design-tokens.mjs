#!/usr/bin/env node
// Keeps src/styles/vendor/openadapt-web in step with the canonical design
// tokens in OpenAdaptAI/openadapt-web.
//
//   --check  (default) fetch the canonical files and fail on any difference
//   --write            fetch the canonical files, rewrite the vendored copies
//                      and provenance.json
//
// --check is the mechanism that keeps the installed Desktop app on the same
// palette as openadapt.ai and app.openadapt.ai. A value that changes upstream,
// or a vendored copy edited by hand, both surface here as a failed build rather
// than as screenshots on the marketing site that no longer match the product.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const vendorDir = path.join(repo, 'src', 'styles', 'vendor', 'openadapt-web');
const provenancePath = path.join(vendorDir, 'provenance.json');

const provenance = JSON.parse(fs.readFileSync(provenancePath, 'utf8'));
const write = process.argv.includes('--write');

const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

async function fetchCanonical(url) {
  const response = await fetch(url, { headers: { accept: 'text/plain' } });
  if (!response.ok) {
    throw new Error(`GET ${url} -> HTTP ${response.status}`);
  }
  return Buffer.from(await response.arrayBuffer());
}

async function canonicalCommit() {
  const url = `https://api.github.com/repos/${provenance.canonical_repository}/commits/${provenance.canonical_branch}`;
  const headers = { accept: 'application/vnd.github+json' };
  if (process.env.GITHUB_TOKEN) headers.authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const response = await fetch(url, { headers });
  if (!response.ok) throw new Error(`GET ${url} -> HTTP ${response.status}`);
  return (await response.json()).sha;
}

const failures = [];

for (const [name, entry] of Object.entries(provenance.files)) {
  const vendoredPath = path.join(vendorDir, name);
  const vendored = fs.readFileSync(vendoredPath);
  const vendoredSha = sha256(vendored);

  if (vendoredSha !== entry.sha256 && !write) {
    failures.push(
      `${name}: the vendored copy was edited by hand.\n` +
        `  provenance.json pins ${entry.sha256}\n` +
        `  the file on disk is  ${vendoredSha}\n` +
        `  Vendored tokens are byte-identical copies. Change the value in ` +
        `${provenance.canonical_repository}:${entry.canonical_path} instead.`,
    );
  }

  const canonical = await fetchCanonical(entry.raw_url);
  const canonicalSha = sha256(canonical);

  if (write) {
    fs.writeFileSync(vendoredPath, canonical);
    entry.sha256 = canonicalSha;
    console.log(`wrote ${name} (${canonicalSha})`);
    continue;
  }

  if (canonicalSha !== vendoredSha) {
    failures.push(
      `${name}: drifted from ${provenance.canonical_repository}@${provenance.canonical_branch}.\n` +
        `  canonical ${entry.canonical_path} is ${canonicalSha}\n` +
        `  the vendored copy is      ${vendoredSha}\n` +
        `  Run: node scripts/vendor-design-tokens.mjs --write`,
    );
  } else {
    console.log(`${name}: matches canonical (${canonicalSha})`);
  }
}

if (write) {
  provenance.vendored_at_commit = await canonicalCommit();
  fs.writeFileSync(provenancePath, `${JSON.stringify(provenance, null, 2)}\n`);
  console.log(`wrote provenance.json at ${provenance.canonical_repository}@${provenance.vendored_at_commit}`);
  process.exit(0);
}

if (failures.length > 0) {
  console.error(`\nVendored design tokens are out of step with ${provenance.canonical_repository}:\n`);
  for (const failure of failures) console.error(`  - ${failure}\n`);
  process.exit(1);
}

console.log(`\nVendored design tokens match ${provenance.canonical_repository}@${provenance.canonical_branch}.`);
