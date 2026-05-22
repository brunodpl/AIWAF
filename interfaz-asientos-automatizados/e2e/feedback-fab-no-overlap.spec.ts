import { test, expect, type Route } from "@playwright/test";

/**
 * Regresión: el FAB "Enviar Problema o Recomendación" no debe solapar
 * los botones de acción primaria en las etapas Revisión (Aprobar) y
 * Exportar (Confirmar y seguir escaneando).
 *
 * Usa exactamente la misma infraestructura de mocks de red que
 * `reviewer-action-target.spec.ts` para alcanzar el stage "review".
 *
 * Para el stage "export" se pre-siembra una factura aprobada en
 * localStorage (mismo patrón de restore() de page.tsx) y se pasa
 * directamente a ese stage.
 */

// ---- datos de mocks compartidos (igual que reviewer-action-target) ----
const RAW_ORDER = ["F-LOTE-1", "F-LOTE-2"] as const;
const BATCH_DOC_IDS = ["F-LOTE-1", "F-LOTE-2"] as const;

function listItem(id: string) {
  return {
    id,
    folder_name: id,
    libro: "ventas",
    status: "review",
    decision_global: "warn",
    timestamp: "2026-05-22T10:00:00Z",
    nif_entidad: "B00000000",
    nombre_entidad: `Entidad ${id}`,
    numero_factura: id,
    total_euros: 100,
    rejection_count: 0,
  };
}

function detail(id: string) {
  return {
    id,
    folder_name: id,
    libro: "ventas",
    status: "review",
    decision_global: "warn",
    metadata: {},
    fields: {
      nombre_entidad: { valor_final: `Entidad ${id}`, decision: "warn", confianza: 0.9 },
      numero_factura: { valor_final: id, decision: "warn", confianza: 0.9 },
      total_euros: { valor_final: "100", decision: "warn", confianza: 0.9 },
    },
    fiscal_lines: [],
    artifacts: [],
    invoice_filename: `${id}.png`,
    rejection_count: 0,
  };
}

/** Registra todas las rutas de red mockeadas necesarias para el reviewer. */
async function setupReviewerMocks(page: import("@playwright/test").Page) {
  // Catch-all (menor prioridad — registrar primero).
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

  await page.route("**/api/pipeline/batch", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        in_flight: true,
        current_file: null,
        current_libro: "ventas",
        books: [
          {
            book_id: "ingresos",
            libro: "ventas",
            label: "Ventas / Ingresos",
            files: BATCH_DOC_IDS.map((doc_id) => ({
              doc_id,
              filename: `${doc_id}.png`,
              status: "done",
            })),
          },
        ],
      }),
    }),
  );

  await page.route(/\/api\/invoices(\/|$|\?)/, async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;

    if (req.method() === "POST" && path.endsWith("/action")) {
      const m = path.match(/\/api\/invoices\/([^/]+)\/action$/);
      const docId = m ? decodeURIComponent(m[1]) : null;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "success", message: "ok", doc_id: docId, new_status: "review" }),
      });
    }

    if (path.endsWith("/file")) {
      return route.fulfill({ status: 200, contentType: "image/png", body: "" });
    }

    const detailMatch = path.match(/\/api\/invoices\/([^/]+)$/);
    if (req.method() === "GET" && detailMatch) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail(decodeURIComponent(detailMatch[1]))),
      });
    }

    // Lista.
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ invoices: RAW_ORDER.map(listItem), total: RAW_ORDER.length }),
    });
  });
}

/** Devuelve true si dos bounding boxes se solapan en cualquier eje. */
function overlaps(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  const aRight = a.x + a.width;
  const aBottom = a.y + a.height;
  const bRight = b.x + b.width;
  const bBottom = b.y + b.height;
  return a.x < bRight && aRight > b.x && a.y < bBottom && aBottom > b.y;
}

// ---------------------------------------------------------------------------
// TEST 1 — Stage Revisión: FAB no solapa el botón "Aprobar"
// ---------------------------------------------------------------------------
test("FAB no solapa el botón Aprobar en el stage Revisión", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("horeca_current_stage", "review");
    localStorage.setItem(
      "horeca_approved_invoices",
      JSON.stringify([
        ["F-LOTE-1", { formData: {}, fiscalLines: [], libro: "ingresos", cuenta_contable: "" }],
      ]),
    );
  });

  await setupReviewerMocks(page);
  await page.goto("/");

  // Esperar a que el reviewer cargue (dot de navegación visible).
  const dot = page.getByRole("button", { name: `Ir a factura F-LOTE-1` });
  await expect(dot).toBeVisible({ timeout: 30_000 });

  // Esperar al botón Aprobar / Aprobada (botón primario del reviewer).
  // Cuando la factura está pre-aprobada el texto es "Aprobada"; de lo contrario,
  // "Aprobar". Ambos comparten posición y es lo que queremos verificar.
  const aprobarBtn = page.getByRole("button", { name: /^Apro(bar|bada)$/ }).last();
  await expect(aprobarBtn).toBeVisible({ timeout: 15_000 });

  // El FAB se identifica por su aria-label.
  const fab = page.getByRole("button", { name: /Enviar Problema/i });
  await expect(fab).toBeVisible({ timeout: 10_000 });

  const fabBox = await fab.boundingBox();
  const aprobarBox = await aprobarBtn.boundingBox();

  expect(fabBox, "No se pudo obtener el bounding box del FAB").not.toBeNull();
  expect(aprobarBox, "No se pudo obtener el bounding box del botón Aprobar").not.toBeNull();

  const doesOverlap = overlaps(fabBox!, aprobarBox!);
  expect(
    doesOverlap,
    `El FAB (y=${fabBox!.y}, h=${fabBox!.height}) solapa el botón Aprobar (y=${aprobarBox!.y}, h=${aprobarBox!.height})`,
  ).toBe(false);
});

// ---------------------------------------------------------------------------
// TEST 2 — Stage Exportar: FAB no solapa "Confirmar y seguir escaneando"
// ---------------------------------------------------------------------------
test("FAB no solapa el botón Confirmar en el stage Exportar", async ({ page }) => {
  // Pre-sembrar stage=export con una factura aprobada para que page.tsx
  // restaure directamente al stage exportar.
  await page.addInitScript(() => {
    localStorage.setItem("horeca_current_stage", "export");
    localStorage.setItem(
      "horeca_approved_invoices",
      JSON.stringify([
        [
          "F-LOTE-1",
          {
            formData: {
              nombre_entidad: "Test SL",
              numero_factura: "F001",
              total_euros: "100",
              nif_entidad: "B00000001",
              fecha_operacion: "2026-01-01",
              fecha_expedicion: "2026-01-01",
              cuenta_contable: "700",
              concepto: "VENTAS",
            },
            fiscalLines: [{ id: "1", base: 100, vatRate: 21, vatAmount: 21 }],
            libro: "ingresos",
            cuenta_contable: "700",
          },
        ],
      ]),
    );
  });

  await setupReviewerMocks(page);

  // También mockear confirm para que no falle si se clica.
  await page.route("**/api/pipeline/confirm", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, facturas_confirmadas: 1, clientes_nuevos: 0 }),
    }),
  );

  await page.goto("/");

  // Esperar al botón de Confirmar en el footer de ExportStage.
  const confirmarBtn = page.getByRole("button", { name: /Confirmar/i });
  await expect(confirmarBtn).toBeVisible({ timeout: 30_000 });

  const fab = page.getByRole("button", { name: /Enviar Problema/i });
  await expect(fab).toBeVisible({ timeout: 10_000 });

  const fabBox = await fab.boundingBox();
  const confirmarBox = await confirmarBtn.boundingBox();

  expect(fabBox, "No se pudo obtener el bounding box del FAB").not.toBeNull();
  expect(confirmarBox, "No se pudo obtener el bounding box del botón Confirmar").not.toBeNull();

  const doesOverlap = overlaps(fabBox!, confirmarBox!);
  expect(
    doesOverlap,
    `El FAB (y=${fabBox!.y}, h=${fabBox!.height}) solapa el botón Confirmar (y=${confirmarBox!.y}, h=${confirmarBox!.height})`,
  ).toBe(false);
});
