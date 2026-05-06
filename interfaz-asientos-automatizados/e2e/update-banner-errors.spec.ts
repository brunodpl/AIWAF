import { test, expect, type Page } from "@playwright/test";

// Tests del banner de actualización: cada código de error del backend debe
// mostrar mensaje correcto + botón Reintentar (donde aplica). Toda la red
// se intercepta — no requieren docker-compose ni backend levantado.

const fakeLatest = {
  version: "99.0.0",
  current: "0.0.0-dev",
  update_available: true,
  changelog: "Test E2E",
  released_at: "2026-05-04T00:00:00Z",
};

async function mockUpdate(
  page: Page,
  status: number,
  body: Record<string, unknown>,
) {
  await page.route("**/api/system/latest-version**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fakeLatest),
    }),
  );
  await page.route("**/api/system/update", (route) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    }),
  );
}

test.describe("Update banner — discriminated error codes", () => {
  test("watchtower_unreachable shows specific message + retry", async ({ page }) => {
    await mockUpdate(page, 503, {
      detail: { code: "watchtower_unreachable", message: "down" },
    });
    await page.goto("/");
    const banner = page.getByTestId("update-banner");
    await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
    const errEl = banner.getByTestId("update-error");
    await expect(errEl).toBeVisible();
    await expect(errEl).toHaveAttribute("data-error-code", "watchtower_unreachable");
    await expect(errEl).toContainText(/Reinicia Docker Desktop/i);
    await expect(banner.getByRole("button", { name: /Reintentar/i })).toBeVisible();
  });

  test("pipeline_busy shows specific message + retry", async ({ page }) => {
    await mockUpdate(page, 409, {
      detail: { code: "pipeline_busy", message: "Pipeline en curso" },
    });
    await page.goto("/");
    const banner = page.getByTestId("update-banner");
    await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
    const errEl = banner.getByTestId("update-error");
    await expect(errEl).toHaveAttribute("data-error-code", "pipeline_busy");
    await expect(errEl).toContainText(/facturas procesándose/i);
    await expect(banner.getByRole("button", { name: /Reintentar/i })).toBeVisible();
  });

  test("auth_failed shows message but NO retry (needs reinstall)", async ({ page }) => {
    await mockUpdate(page, 502, {
      detail: { code: "auth_failed", message: "Token rechazado" },
    });
    await page.goto("/");
    const banner = page.getByTestId("update-banner");
    await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
    const errEl = banner.getByTestId("update-error");
    await expect(errEl).toHaveAttribute("data-error-code", "auth_failed");
    await expect(errEl).toContainText(/Contacta a soporte/i);
    await expect(banner.getByRole("button", { name: /Reintentar/i })).toHaveCount(0);
  });

  test("retry button returns to idle and re-enables Actualizar", async ({ page }) => {
    await mockUpdate(page, 503, {
      detail: { code: "watchtower_unreachable", message: "down" },
    });
    await page.goto("/");
    const banner = page.getByTestId("update-banner");
    await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
    await banner.getByRole("button", { name: /Reintentar/i }).click();
    await expect(banner.getByRole("button", { name: /Actualizar ahora/i })).toBeVisible();
  });

  test("legacy backend (string detail) maps to unknown code with retry", async ({ page }) => {
    await mockUpdate(page, 502, { detail: "Legacy plain string error" });
    await page.goto("/");
    const banner = page.getByTestId("update-banner");
    await banner.getByRole("button", { name: /Actualizar ahora/i }).click();
    const errEl = banner.getByTestId("update-error");
    await expect(errEl).toHaveAttribute("data-error-code", "unknown");
  });
});
