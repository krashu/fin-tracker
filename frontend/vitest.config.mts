import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // D8: a fixed cap of two workers. The default (CPU count − 1, about 13 on a 14-core laptop)
    // oversubscribes the machine, and the first test in the heavy happy-dom dialog files then
    // times out at 5 s. Two is the only count proven green, and it fits any GitHub runner
    // (public 4-vCPU or private 2-vCPU). Never raise testTimeout or a per-test timeout to
    // hide a slow test; make the test cheaper instead.
    maxWorkers: 2,
    environment: "happy-dom",
    setupFiles: ["./tests/setup.ts"],
    include: ["{app,components,lib,tests}/**/*.test.{ts,tsx}"],
    exclude: ["**/node_modules/**", "**/.next/**"],
  },
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "."),
    },
  },
});
