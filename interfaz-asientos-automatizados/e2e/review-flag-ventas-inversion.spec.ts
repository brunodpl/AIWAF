import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * E2E del Bug 1 (Luckia / inversión emisor-receptor): una factura de VENTAS cuyo
 * emisor no es cliente conocido (y el receptor sí) podía autocargarse en silencio
 * con el cliente equivocado. Tras el fix, el backend la marca para revisión
 * (decision warn → status "review"), así que en el lote aparece como "Revisión"
 * y NO como "Hecha"/auto — el operario la verá y corregirá.
 *
 * No necesita backend ni Gemini: mockea `/api/**`. La pantalla de progreso se
 * alcanza vía el restore offline-first (pending_confirm=true, status=running).
 *
 * Verifica el comportamiento observable: la factura de ventas problemática queda
 * en "Revisión" (no se procesa como auto/Hecha en silencio).
 */

const STARTED = "2026-05-27T14:49:00Z";

// Lote de ventas: la factura "Luckia" queda en revisión (status=review); una
// venta normal queda "done". El backend mapea decision warn → status review.
const BATCH_BODY = {
  in_flight: true,
  current_file: null,
  current_libro: "ventas",
  books: [
    {
      book_id: "ingresos",
      libro: "ventas",
      label: "Ventas",
      files: [
        { doc_id: "v_luckia", filename: "luckia_maria_concepcion.pdf", status: "review", decision: "warn" },
        { doc_id: "v_ok", filename: "venta_normal.pdf", status: "done", decision: "auto" },
      ],
    },
  ],
};

// status: se mantiene "running" (pending_confirm) — la pantalla de escaneo es
// estable y muestra el lote con la factura en revisión.
const STATUS_BODY = {
  status: "running",
  processed: 1,
  total: 2,
  current_file: "luckia_maria_concepcion.pdf",
  started_at: STARTED,
  completed_at: null,
  error_message: null,
  pending_confirm: true,
  pending_doc_ids: ["v_luckia", "v_ok"],
};

async function mockBatch(page: Page) {
  await page.route("**/api/**", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
  await page.route("**/api/system/version", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ version: "0.0.0-test", gestoria_nif: "B0", gestoria_nombre: "Test" }),
    }),
  );
  await page.route("**/api/system/latest-version**", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ version: "0.0.0-test", current: "0.0.0-test", update_available: false }),
    }),
  );
  await page.route("**/api/pipeline/status", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(STATUS_BODY) }),
  );
  await page.route("**/api/pipeline/batch", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(BATCH_BODY) }),
  );
}

test("una venta con posible inversión emisor/receptor se marca Revisión, no auto", async ({ page }) => {
  await mockBatch(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /Escaneando facturas/i })).toBeVisible({ timeout: 30_000 });

  // La factura de Luckia aparece y queda en "Revisión" (no se autocarga).
  // Acotamos a su fila <li>: "Revisión" también es el nombre de un stage en la
  // barra superior (StageIndicator), así que getByText global es ambiguo.
  const luckiaRow = page.locator("li", { has: page.locator('[title="luckia_maria_concepcion.pdf"]') });
  await expect(luckiaRow).toBeVisible();
  await expect(luckiaRow).toContainText("Revisión");

  // La venta normal sí está "Hecha": el flag de revisión es específico de la
  // factura problemática, no un bloqueo del lote entero.
  const okRow = page.locator("li", { has: page.locator('[title="venta_normal.pdf"]') });
  await expect(okRow).toBeVisible();
  await expect(okRow).toContainText("Hecha");
});
