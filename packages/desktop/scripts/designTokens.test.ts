// Offline guards on the vendored canonical design tokens.
//
// The published cockpit screenshots went stale because the app kept a private
// copy of a palette the rest of the product had retired, and nothing failed
// when the two diverged. These tests fail instead.
//
// `node scripts/vendor-design-tokens.mjs --check` is the online half: it fails
// when the vendored copy differs from openadapt-web@main. These three run with
// no network, on every `npm run test:ui`.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..");
const vendorDir = path.join(repo, "src", "styles", "vendor", "openadapt-web");

const provenance = JSON.parse(
  fs.readFileSync(path.join(vendorDir, "provenance.json"), "utf8"),
) as {
  canonical_repository: string;
  files: Record<string, { canonical_path: string; sha256: string }>;
};

const canonicalTokens = JSON.parse(
  fs.readFileSync(path.join(vendorDir, "tokens.json"), "utf8"),
) as { color: Record<string, string> };

// Every stylesheet the app or the engine actually ships.
const STYLESHEETS = [
  "src/styles/tokens.css",
  "src/styles/app.css",
  "src/overlay/overlay.css",
  "engine/portal/shell/styles.css",
];

function read(relative: string): string {
  return fs.readFileSync(path.join(repo, relative), "utf8");
}

describe("vendored canonical design tokens", () => {
  it("are byte-identical to the copies provenance.json pins", () => {
    for (const [name, entry] of Object.entries(provenance.files)) {
      const bytes = fs.readFileSync(path.join(vendorDir, name));
      const digest = crypto.createHash("sha256").update(bytes).digest("hex");
      expect(
        digest,
        `${name} was hand-edited. Vendored tokens are byte-identical copies of `
          + `${provenance.canonical_repository}:${entry.canonical_path}. Change the `
          + `value there, then run: node scripts/vendor-design-tokens.mjs --write`,
      ).toBe(entry.sha256);
    }
  });

  it("are the only place a canonical colour is defined", () => {
    // Desktop keeps its own spacing and type scales on purpose (see
    // src/styles/tokens.css section 5). Colour is the thing that must not fork:
    // a second definition of --surface or --accent-verified is exactly how the
    // app and the marketing site end up looking like different products.
    const local = read("src/styles/tokens.css");
    const redefined = Object.keys(canonicalTokens.color).filter((token) =>
      new RegExp(`^\\s*${token}\\s*:`, "m").test(local),
    );
    expect(
      redefined,
      "src/styles/tokens.css redefines canonical colour tokens. Derive from "
        + "them with var() or color-mix() instead.",
    ).toEqual([]);
  });

  it("leave no retired warm-palette value in any shipped stylesheet", () => {
    // The palette openadapt-cloud retired in its PR #325. These exact values
    // are what the eight published cockpit screenshots were shot on.
    const RETIRED = [
      "#f2f1ec", "#fbfaf6", "#eae8e0", "#e0ded4", "#dddcd2", "#c9ccc2",
      "#1a1e17", "#23281f", "#4c523f", "#5a6050",
      "#3e6b4f", "#2f5340", "#5c9e77",
      "#1f9d57", "#b5822a", "#c0453a", "#3a6e8c",
      "#f4f3ed", "#fffef9", "#eeede5", "#d6d8ce", "#252a22", "#687066",
      "#2f7154", "#a66a25", "#2e7c5a", "#a34f4c", "#356f9f",
    ];
    for (const sheet of STYLESHEETS) {
      const text = read(sheet).toLowerCase();
      const found = RETIRED.filter((value) => text.includes(value));
      expect(found, `${sheet} still carries retired warm-palette values`).toEqual([]);
    }
  });
});
