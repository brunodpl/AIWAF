import { test, expect } from "@playwright/test";

// Test diagnóstico: NO se ejecuta por defecto (excluido en testIgnore).
// Forzar con: npx playwright test e2e/_diagnostic/ --grep "diagnose"
//
// Requiere docker-compose levantado (./docker compose up -d). Forza el banner
// interceptando latest-version y deja que la llamada real al backend
// /api/system/update llegue a Watchtower; captura status + body para diagnóstico.

test("diagnose update flow against real backend", async ({ page }) => {
  await page.route("**/api/system/latest-version**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version: "99.0.0",
        current: "0.0.0-dev",
        update_available: true,
        changelog: "Diagnostic E2E",
        released_at: "2026-05-04T00:00:00Z",
      }),
    }),
  );

  const updatePromise = page.waitForResponse("**/api/system/update");
  await page.goto("/");
  await page.getByRole("button", { name: /Actualizar ahora/i }).click();

  const resp = await updatePromise;
  const status = resp.status();
  const body = await resp.text();
  console.log("=========================================");
  console.log("[diagnose] /api/system/update status:", status);
  console.log("[diagnose] body:", body);
  console.log("=========================================");

  // No assertion sobre éxito — el objetivo es diagnosticar. Marca verde si la
  // request completó (200/4xx/5xx); marca rojo si timeout o sin respuesta.
  expect([200, 409, 500, 502, 503]).toContain(status);
});
