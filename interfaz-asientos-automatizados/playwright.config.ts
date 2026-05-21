import { defineConfig, devices } from "@playwright/test";

// Tests E2E del flujo de actualización (banner → POST /api/system/update → verificación versión).
// Ejecutar contra el frontend dev (npm run dev en :3000) o el contenedor (puerto 3003).
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000";

export default defineConfig({
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
    {
      name: "chromium",
      testDir: "./e2e",
      testIgnore: ["**/_diagnostic/**", "**/multi-invoice/**"],
      use: { ...devices["Desktop Chrome"] },
    },
    // Suite multi-invoice — llamadas reales a Gemini sobre el sistema en :3003.
    // Sin workers paralelos: comparten estado en libros/asientos/.
    // Timeout amplio (10 min) porque el splitter + 3 facturas con Gemini real
    // puede tardar varios minutos. Retries=0: si Gemini falla, queremos verlo.
    {
      name: "multi-invoice",
      testDir: "./e2e/multi-invoice",
      testMatch: /.*\.spec\.ts$/,
      timeout: 10 * 60 * 1000,
      expect: { timeout: 60_000 },
      fullyParallel: false,
      retries: 0,
      // Forzamos 1 worker en este proyecto. Sin esto, Playwright corre los
      // ficheros .spec.ts en paralelo (un worker por fichero), lo que rompe
      // la suite porque comparten estado en libros/asientos/.
      workers: 1,
      use: {
        baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3003",
        ...devices["Desktop Chrome"],
        trace: "on",
        screenshot: "on",
        video: "retain-on-failure",
      },
    },
  ],
});
