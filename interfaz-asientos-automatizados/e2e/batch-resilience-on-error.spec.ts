import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * E2E del Bug 2 (lote colgado): "cuando una factura da error se queda cargando
 * y no pasa a la siguiente factura". La causa raíz era una llamada a Gemini sin
 * timeout (un cuelgue no lanza excepción → el bucle síncrono quedaba bloqueado).
 * Tras el fix, una factura que falla queda como `error` y el lote CONTINÚA con
 * las siguientes.
 *
 * No necesita backend ni Gemini: mockea `/api/**`. La pantalla de progreso
 * (PipelineProgress + BatchOverview) se alcanza vía el restore offline-first de
 * app/page.tsx: con `pending_confirm=true` y `status=running` entra a "processing".
 *
 * Verifica el comportamiento observable y LITERAL del bug: la factura nº2 (error)
 * NO detiene el lote — la nº3 (POSTERIOR a la fallida) aparece "Hecha". Asertamos
 * sobre el estado estable `running` (no sobre el badge efímero "Completado", que
 * solo se ve ~800ms antes de navegar a revisión → sería flaky).
 */

const STARTED = "2026-05-27T16:05:00Z";

// Lote: 3 facturas de compras. La nº2 falló (error); la nº1 y la nº3 están
// hechas. Que la nº3 (posterior a la fallida) esté "Hecha" demuestra que el lote
// no se detuvo en el fallo, sino que pasó a la siguiente.
const BATCH_BODY = {
  in_flight: true,
  current_file: null,
  current_libro: "compras",
  books: [
    {
      book_id: "gastos",
      libro: "compras",
      label: "Compras",
      files: [
        { doc_id: "f1", filename: "factura_1.pdf", status: "done" },
        { doc_id: "f2", filename: "factura_2.pdf", status: "error" },
        { doc_id: "f3", filename: "factura_3.pdf", status: "done" },
      ],
    },
  ],
};

// status estable "running": la pantalla de escaneo no se desmonta, así que las
// aserciones sobre el lote son deterministas.
const STATUS_BODY = {
  status: "running",
  processed: 3,
  total: 3,
  current_file: null,
  started_at: STARTED,
  completed_at: null,
  error_message: null,
  pending_confirm: true,
  pending_doc_ids: ["f1", "f2", "f3"],
};

async function mockBatch(page: Page) {
  // Catch-all primero (menor prioridad): cualquier /api/** no específico → {}.
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

test("una factura con error no detiene el lote: la siguiente se procesa igualmente", async ({ page }) => {
  await mockBatch(page);
  await page.goto("/");

  // El restore (pending_confirm + running) entra a la pantalla de escaneo.
  await expect(page.getByRole("heading", { name: /Escaneando facturas/i })).toBeVisible({ timeout: 30_000 });

  // La factura nº2 falló y se marca "Error" (acotado a su fila <li>).
  const errorRow = page.locator("li", { has: page.locator('[title="factura_2.pdf"]') });
  await expect(errorRow).toBeVisible();
  await expect(errorRow).toContainText("Error");

  // La factura nº3, POSTERIOR a la fallida, está "Hecha": el lote continuó tras
  // el error en vez de quedarse colgado (este es el bug literal reportado).
  const afterErrorRow = page.locator("li", { has: page.locator('[title="factura_3.pdf"]') });
  await expect(afterErrorRow).toBeVisible();
  await expect(afterErrorRow).toContainText("Hecha");

  // Y la nº1 (anterior) también está hecha: el lote procesó todas menos la fallida.
  const beforeErrorRow = page.locator("li", { has: page.locator('[title="factura_1.pdf"]') });
  await expect(beforeErrorRow).toContainText("Hecha");
});
