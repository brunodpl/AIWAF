import { defineConfig, devices } from "@playwright/test";

// Smoke gate HERMÉTICO para el pipeline de release (lo invoca la skill
// aiwaf-release vía `npm run test:e2e:smoke`).
//
// Por qué un config aparte del playwright.config.ts compartido: la release
// corre en la máquina de Bruno, donde a menudo hay un `npm run dev` en :3000.
// Aquí construimos el build de PRODUCCIÓN y lo servimos en un puerto dedicado
// (:3100) con reuseExistingServer:false → mismo resultado pase lo que pase en
// :3000. El build de producción no recompila sobre la marcha (sin HMR), así que
// el window.location.reload() post-actualización no puede colgar el servidor
// (esa era la causa raíz del flake con el dev server).
//
// Toda la red la mockean los specs, así que NO hace falta backend; el build
// de Next pasa standalone sin pipeline-api levantado.

const PORT = 3100;

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["**/update.spec.ts"],
  timeout: 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: `npm run build && npm run start -- -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 300_000,
    // distDir aislado + sin cache de webpack → el build del gate NO toca el
    // `.next` del `next dev` (evita la corrupción de cache que colgaba el build).
    env: {
      NEXT_DIST_DIR: ".next-smoke",
      NEXT_DISABLE_WEBPACK_CACHE: "1",
      NEXT_DISABLE_STANDALONE: "1",
    },
  },
});
