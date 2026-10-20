import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// The app bundles two SIL OFL 1.1 fonts (src/styles/fonts/SOURCES.md). The
// licence has to travel with every copy of a font, so the built frontend
// carries both licence files next to the fonts it ships.
const FONT_LICENCES = ["OFL-PublicSans.txt", "OFL-SourceSerif4.txt"];

function fontLicences(): Plugin {
  return {
    name: "openadapt-font-licences",
    apply: "build",
    generateBundle() {
      for (const name of FONT_LICENCES) {
        this.emitFile({
          type: "asset",
          fileName: `licenses/${name}`,
          source: readFileSync(new URL(`./src/styles/fonts/${name}`, import.meta.url), "utf8"),
        });
      }
    },
  };
}

// Desktop imports the shared program map from its source in the monorepo
// (packages/program-graph). React resolves from Desktop's own node_modules, so
// the app bundles one copy of it.
export const PROGRAM_GRAPH_ALIAS = [
  {
    find: /^@openadapt\/program-graph\/styles\.css$/,
    replacement: fileURLToPath(new URL("../program-graph/src/styles.css", import.meta.url)),
  },
  {
    find: /^@openadapt\/program-graph$/,
    replacement: fileURLToPath(new URL("../program-graph/src/index.ts", import.meta.url)),
  },
];

// Vite config tuned for Tauri (see https://v2.tauri.app/start/frontend/vite/).
// - fixed dev port so `devUrl` in tauri.conf.json can point at it
// - don't clear the screen so Rust/cargo logs remain visible
// - build output to ../dist (frontendDist in tauri.conf.json)
export default defineConfig({
  plugins: [react(), fontLicences()],
  resolve: {
    alias: PROGRAM_GRAPH_ALIAS,
    dedupe: ["react", "react-dom"],
  },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    fs: { allow: [".", "../program-graph"] },
  },
  build: {
    outDir: "dist",
    target: "es2021",
    sourcemap: false,
    emptyOutDir: true,
  },
});
