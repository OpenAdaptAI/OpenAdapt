import { defineConfig } from "vitest/config";
import { PROGRAM_GRAPH_ALIAS } from "./vite.config";

export default defineConfig({
  resolve: {
    alias: PROGRAM_GRAPH_ALIAS,
    dedupe: ["react", "react-dom"],
  },
  test: {
    environment: "jsdom",
    // scripts/ carries the cockpit capture contract, which is a test even
    // though the harness it guards is not application code.
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
  },
});
