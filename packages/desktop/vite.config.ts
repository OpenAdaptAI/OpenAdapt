import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite config tuned for Tauri (see https://v2.tauri.app/start/frontend/vite/).
// - fixed dev port so `devUrl` in tauri.conf.json can point at it
// - don't clear the screen so Rust/cargo logs remain visible
// - build output to ../dist (frontendDist in tauri.conf.json)
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    target: "es2021",
    sourcemap: false,
    emptyOutDir: true,
  },
});
