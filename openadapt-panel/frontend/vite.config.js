import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI app serves index.html at "/" and mounts built assets at
// "/static", so assets must be referenced under that base. Output goes
// straight into the Python package's static/ dir, which the wheel ships.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "../openadapt_panel/static",
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
