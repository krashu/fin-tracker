import path from "node:path";
import { defineConfig } from "vitest/config";

// Vitest does not read tsconfig.json's `paths`, so the `@/*` alias used
// throughout app code (see tsconfig.json's `paths`) needs restating here or
// every `@/lib/...` import in a test file fails to resolve. `@` maps to the
// frontend project root, matching `"@/*": ["./*"]` exactly — kept to this one
// alias, no extra package (e.g. vite-tsconfig-paths) pulled in for it.
export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "."),
    },
  },
});
