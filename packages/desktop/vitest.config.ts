import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    // scripts/ carries the cockpit capture contract, which is a test even
    // though the harness it guards is not application code.
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
  },
});
