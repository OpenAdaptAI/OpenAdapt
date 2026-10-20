#!/usr/bin/env node
// Build the ESM distribution: tsc emits dist/*.js and dist/*.d.ts, and the
// stylesheet is copied next to them. The output is deterministic, so the
// vendor manifest (scripts/vendor.mjs) can pin it by checksum.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
fs.rmSync(dist, { recursive: true, force: true });
const tsc = path.join(root, "node_modules", ".bin", process.platform === "win32" ? "tsc.cmd" : "tsc");
execFileSync(tsc, ["-p", path.join(root, "tsconfig.build.json")], { stdio: "inherit" });
fs.copyFileSync(path.join(root, "src", "styles.css"), path.join(dist, "styles.css"));

// Zero runtime dependencies beyond React: refuse a build that imports
// anything else.
const allowed = new Set(["react", "react/jsx-runtime"]);
for (const file of fs.readdirSync(dist).filter((name) => name.endsWith(".js"))) {
  const source = fs.readFileSync(path.join(dist, file), "utf8");
  for (const [, spec] of source.matchAll(/\bfrom\s+["']([^"']+)["']/g)) {
    if (spec.startsWith("./")) {
      if (!fs.existsSync(path.join(dist, spec))) throw new Error(`${file} imports ${spec}, which the build did not emit`);
      continue;
    }
    if (!allowed.has(spec)) throw new Error(`${file} imports ${spec}; the package may import only React`);
  }
}
console.log(`built ${path.relative(process.cwd(), dist)}`);
