import { defineConfig, devices } from "@playwright/test";

// Configuración HERMÉTICA para el gate de CI (.github/workflows/ci.yml).
// Generaliza playwright.smoke.config.ts: construye el build de producción
// y lo sirve en un puerto dedicado (:3110), con TODA la red mockeada por
// los propios specs → no necesita pipeline-api ni Gemini.
//
// Corre los 4 specs top-level de e2e/ (update, export-fiscal-warning,
// reviewer-action-target, feedback-fab-no-overlap). Excluye:
//   - multi-invoice/ → suite cara con Gemini real (manual/nocturna)
//   - _diagnostic/   → herramientas de debug, no son tests de gate
//
// distDir aislado (.next-ci) + webpack cache desactivado para no chocar
// con un `next dev` o `next build` que el dev pueda tener corriendo.

const PORT = 3110;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.spec.ts",
  testIgnore: ["**/multi-invoice/**", "**/_diagnostic/**"],
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["github"], ["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: `pnpm run build && pnpm exec next start -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 300_000,
    env: {
      NEXT_DIST_DIR: ".next-ci",
      NEXT_DISABLE_WEBPACK_CACHE: "1",
      NEXT_DISABLE_STANDALONE: "1",
    },
  },
});
