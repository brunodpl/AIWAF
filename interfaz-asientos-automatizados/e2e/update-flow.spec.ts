import { test, expect } from "@playwright/test";

// E2E del flujo completo de actualización con red mockeada.
// Simula: nueva versión publicada → banner aparece → click Actualizar →
// backend devuelve 200 → polling detecta versión nueva → estado "success".
//
// Para validar contra contenedores reales, ver e2e/_diagnostic/diagnose-update.spec.ts
// y la sección "Verificación manual con instalador real" en docs/superpowers/plans/.

const NEW_VERSION = "99.0.0";
const OLD_VERSION = "0.0.0-dev";

test("flujo completo: banner → actualizar → success → recarga", async ({ page }) => {
  let postUpdateCalls = 0;
  let versionCalls = 0;

  await page.route("**/api/system/latest-version**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version: NEW_VERSION,
        current: OLD_VERSION,
        update_available: true,
        changelog: "Test E2E flujo completo",
        released_at: "2026-05-04T00:00:00Z",
      }),
    }),
  );

  // POST /api/system/update — simula que Watchtower acepta
  await page.route("**/api/system/update", (route) => {
    postUpdateCalls += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "requested",
        eta_seconds: 90,
        message: "Actualización iniciada. Tardará 1-3 minutos.",
      }),
    });
  });

  // GET /api/system/version — primeras 2 llamadas devuelven OLD, luego NEW
  // (simula contenedores reiniciándose y el nuevo arrancando).
  await page.route("**/api/system/version", (route) => {
    versionCalls += 1;
    const version = versionCalls >= 3 ? NEW_VERSION : OLD_VERSION;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version,
        gestoria_nif: "B00000000",
        gestoria_nombre: "Test Gestoría",
      }),
    });
  });

  await page.goto("/");
  const banner = page.getByTestId("update-banner");
  await expect(banner).toContainText(
    new RegExp(`Actualización disponible:\\s*${NEW_VERSION}`),
    { timeout: 30_000 },
  );

  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
  await expect(banner).toContainText(/Actualizando…/i);
  expect(postUpdateCalls).toBe(1);

  // El polling pulsa cada 5s, timeout 5min. Damos 30s margen para 3 ciclos.
  await expect(banner).toContainText(new RegExp(`Actualizado a ${NEW_VERSION}`), {
    timeout: 30_000,
  });
});

test("flujo con error: 503 watchtower_unreachable → mensaje + reintentar funciona", async ({ page }) => {
  let updateAttempts = 0;

  await page.route("**/api/system/latest-version**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        version: NEW_VERSION,
        current: OLD_VERSION,
        update_available: true,
        changelog: "Test",
        released_at: "2026-05-04T00:00:00Z",
      }),
    }),
  );

  await page.route("**/api/system/update", (route) => {
    updateAttempts += 1;
    if (updateAttempts === 1) {
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            code: "watchtower_unreachable",
            message: "El servicio de actualizaciones no responde.",
          },
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "requested",
        eta_seconds: 90,
        message: "Actualización iniciada.",
      }),
    });
  });

  await page.goto("/");
  const banner = page.getByTestId("update-banner");
  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();

  const errEl = banner.getByTestId("update-error");
  await expect(errEl).toHaveAttribute("data-error-code", "watchtower_unreachable");
  await expect(errEl).toContainText(/Reinicia Docker Desktop/i);

  await banner.getByRole("button", { name: /Reintentar/i }).click();
  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
  await expect(banner).toContainText(/Actualizando…/i);
  expect(updateAttempts).toBe(2);
});
