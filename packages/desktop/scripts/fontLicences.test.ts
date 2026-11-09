// Every font the app bundles carries its licence, in the repository and in
// the built frontend. The SIL OFL 1.1 requires the licence to travel with
// each copy of the font, and the built app is a copy.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fontsDir = path.join(repo, "src", "styles", "fonts");

describe("bundled fonts", () => {
  const fonts = fs.readdirSync(fontsDir).filter((name) => name.endsWith(".woff2"));
  const licences = fs.readdirSync(fontsDir).filter((name) => /^OFL-.+\.txt$/.test(name));

  it("each has an OFL licence file and a row in SOURCES.md", () => {
    const sources = fs.readFileSync(path.join(fontsDir, "SOURCES.md"), "utf8");
    expect(fonts.length).toBeGreaterThan(0);
    for (const font of fonts) {
      const family = font.replace(/-Variable\.woff2$/, "");
      expect(licences, `${font} needs OFL-${family}.txt`).toContain(`OFL-${family}.txt`);
      expect(sources, `${font} needs a row in SOURCES.md`).toContain(font);
    }
  });

  it("ship their licences in the built app", () => {
    const config = fs.readFileSync(path.join(repo, "vite.config.ts"), "utf8");
    for (const licence of licences) {
      expect(config, `vite.config.ts must emit ${licence}`).toContain(`"${licence}"`);
    }
  });
});
