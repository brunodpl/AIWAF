import { test, expect, type Page } from "@playwright/test";

// Smoke gate del flujo de actualización (banner → POST /api/system/update →
// verificación de versión). Toda la red se intercepta: NO requiere backend ni
// docker-compose. Se ejecuta contra el build de producción que levanta
// playwright.smoke.config.ts en un puerto dedicado (ver ese fichero).

const NEW_VERSION = "99.0.0";
// Versión "instalada" que simula una gestoría en producción. NO usar el centinela
// "0.0.0-dev": el banner se oculta cuando current === DEV_VERSION (guard de build
// dev en update-banner.tsx), y todos estos tests dejarían de ver el banner.
const OLD_VERSION = "0.9.0";

const fakeLatest = {
  version: NEW_VERSION,
  current: OLD_VERSION,
  update_available: true,
  changelog: "Test E2E",
  released_at: "2026-05-04T00:00:00Z",
};

async function mockLatest(page: Page) {
  await page.route("**/api/system/latest-version**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fakeLatest),
    }),
  );
}

// ── Caminos de error ────────────────────────────────────────────────

test("error recuperable (watchtower_unreachable): mensaje + Reintentar → vuelve a idle", async ({
  page,
}) => {
  await mockLatest(page);
  await page.route("**/api/system/update", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "watchtower_unreachable", message: "down" },
      }),
    }),
  );

  await page.goto("/");
  const banner = page.getByTestId("update-banner");
  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();

  const errEl = banner.getByTestId("update-error");
  await expect(errEl).toBeVisible();
  await expect(errEl).toHaveAttribute("data-error-code", "watchtower_unreachable");
  await expect(errEl).toContainText(/Reinicia Docker Desktop/i);

  // Reintentar devuelve el banner al estado inicial (botón Actualizar de nuevo).
  await banner.getByRole("button", { name: /Reintentar/i }).click();
  await expect(banner.getByRole("button", { name: /Actualizar ahora/i })).toBeVisible();
});

test("auth_failed: mensaje SIN Reintentar (requiere reinstalar)", async ({ page }) => {
  await mockLatest(page);
  await page.route("**/api/system/update", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "auth_failed", message: "Token rechazado" },
      }),
    }),
  );

  await page.goto("/");
  const banner = page.getByTestId("update-banner");
  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();

  const errEl = banner.getByTestId("update-error");
  await expect(errEl).toHaveAttribute("data-error-code", "auth_failed");
  await expect(errEl).toContainText(/Contacta a soporte/i);
  await expect(banner.getByRole("button", { name: /Reintentar/i })).toHaveCount(0);
});

test("backend legacy (detail string) → código unknown", async ({ page }) => {
  await mockLatest(page);
  await page.route("**/api/system/update", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Legacy plain string error" }),
    }),
  );

  await page.goto("/");
  const banner = page.getByTestId("update-banner");
  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();

  const errEl = banner.getByTestId("update-error");
  await expect(errEl).toHaveAttribute("data-error-code", "unknown");
});

// ── Camino feliz ─────────────────────────────────────────────────────
// Va el ÚLTIMO a propósito: al alcanzar `success` el banner programa un
// window.location.reload() a +3s. Lo neutralizamos con addInitScript para que
// no navegue la página, y aun así lo dejamos al final para que ningún test
// dependa de su orden.

test("flujo completo: banner → actualizar → success", async ({ page }) => {
  let postUpdateCalls = 0;
  let versionCalls = 0;

  // Neutraliza la recarga post-actualización: en tests no debe navegar.
  await page.addInitScript(() => {
    const noop = () => {};
    try {
      Object.defineProperty(window.location, "reload", {
        configurable: true,
        value: noop,
      });
    } catch {
      /* si reload no es configurable, el build de producción tolera la recarga */
    }
  });

  await mockLatest(page);
  await page.route("**/api/system/update", (route) => {
    postUpdateCalls += 1;
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
  // Las 2 primeras lecturas devuelven la versión vieja, la 3ª la nueva
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
  );

  await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
  await expect(banner).toContainText(/Actualizando…/i);
  expect(postUpdateCalls).toBe(1);

  await expect(banner).toContainText(new RegExp(`Actualizado a ${NEW_VERSION}`));
});
