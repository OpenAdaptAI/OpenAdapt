import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run demo`: the workbench on the OpenEMR referral fixture and on
// Desktop's preview graph. The visual tests (test/visual/) shoot this page.
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [react()],
  // The fixture's synthetic pictures, served at /recorded/... and /run/...
  publicDir: fileURLToPath(new URL("../fixtures/openemr-referral/media", import.meta.url)),
  server: { port: 4517, strictPort: true, host: "127.0.0.1" },
});
