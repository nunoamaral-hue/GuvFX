import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** WP4.2 — vitest for the Broker Accounts UI. Runs under jsdom via the `prelint` npm hook (so CI's
 * frontend lint job and `make check` execute it); the Docker image build runs only `next build`, so
 * tests do not run in-image. */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    css: false,
  },
});
