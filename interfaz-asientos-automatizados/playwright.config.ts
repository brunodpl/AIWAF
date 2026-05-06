import { defineConfig, devices } from "@playwright/test";

// Tests E2E del flujo de actualización (banner → POST /api/system/update → verificación versión).
// Ejecutar contra el frontend dev (npm run dev en :3000) o el contenedor (puerto 3003).
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["**/_diagnostic/**"],
  timeout: 5 * 60 * 1000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
