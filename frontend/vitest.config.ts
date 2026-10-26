import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // The tests assert requests against the default API address. A developer's own .env (VITE_API_BASE_URL) must not change what they check, so it is pinned here, for tests only.
    env: { VITE_API_BASE_URL: "http://127.0.0.1:8000" },
  },
});
