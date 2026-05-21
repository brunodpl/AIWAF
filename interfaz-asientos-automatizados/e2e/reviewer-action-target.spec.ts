import { test, expect, type Route } from "@playwright/test";

/**
 * Regresión de pérdida de datos fiscales (alta severidad).
 *
 * BUG: el reviewer PINTA la factura `visibleSummaries[currentIdx]` (lista
 * filtrada por lote/foco) pero `executeConfirmedAction` resolvía la factura
 * ACCIONADA vía `invoiceSummaries[currentIdx]` (lista CRUDA). Con un lote
 * activo la lista visible es un subconjunto de la cruda, así que los índices
 * divergen: el operario ve la factura A y al pulsar Rechazar el sistema
 * rechaza la factura B. En una herramienta contable eso confirma/borra el
 * asiento equivocado.
 *
 * Este test fuerza la divergencia con red mockeada (no necesita backend ni
 * Gemini), navega a la 2ª factura VISIBLE, la rechaza, e intercepta el POST
 * de acción para verificar que el doc_id accionado es EXACTAMENTE el que está
 * en pantalla — no el de la lista cruda.
 *
 * Disposición de datos (la clave del test):
 *   /api/invoices (cruda) → [F-OLD-1, F-LOTE-1, F-OLD-2, F-LOTE-2]
 *   /api/pipeline/batch   → lote = {F-LOTE-1, F-LOTE-2}
 *   ⇒ visibleSummaries    = [F-LOTE-1, F-LOTE-2]
 *
 *   visible[1] = F-LOTE-2  ← lo que el operario VE y rechaza
 *   cruda[1]   = F-LOTE-1  ← lo que el bug accionaría (otra factura del lote,
 *                            ya cacheada por haber montado en el índice 0)
 */

const DOC_DISPLAYED = "F-LOTE-2"; // visible[1] — en pantalla
const DOC_WRONG = "F-LOTE-1"; // cruda[1] — el que rechazaría el bug

// Orden de la lista cruda. El filtro de lote preserva este orden.
const RAW_ORDER = ["F-OLD-1", "F-LOTE-1", "F-OLD-2", "F-LOTE-2"] as const;
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
    rejection_count: 0, // 0 ⇒ primer rechazo ⇒ llamada directa al backend (sin hard-delete)
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
    invoice_filename: `${id}.png`, // .png ⇒ ImageViewer usa <img>, no iframe PDF
    rejection_count: 0,
  };
}

test("rechazar acciona la factura VISIBLE en pantalla, no el índice de la lista cruda", async ({
  page,
}) => {
  // Captura del POST de acción: aquí se ve sobre qué doc_id actuó el sistema.
  const actioned: { docId: string | null; body: unknown } = { docId: null, body: null };

  // Pre-sembrar localStorage para que app/page.tsx restaure el stage "review"
  // (restore() exige al menos una factura aprobada presente en /api/invoices).
  // Aprobamos F-OLD-1 (invisible en el lote) para no contaminar la lista visible.
  await page.addInitScript(() => {
    localStorage.setItem("horeca_current_stage", "review");
    localStorage.setItem(
      "horeca_approved_invoices",
      JSON.stringify([
        ["F-OLD-1", { formData: {}, fiscalLines: [], libro: "ingresos", cuenta_contable: "" }],
      ]),
    );
  });

  // Catch-all de /api/** (se registra PRIMERO ⇒ menor prioridad en Playwright,
  // que evalúa las rutas en orden inverso de registro). Evita que peticiones no
  // mockeadas lleguen al proxy de Next y cuelguen el test.
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

  // Lote: define qué facturas son VISIBLES (subconjunto de la cruda).
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

  // Endpoints de facturas: lista, detalle, fichero y acción. Un único router
  // por RegExp para evitar ambigüedades de precedencia entre globs solapados.
  await page.route(/\/api\/invoices(\/|$|\?)/, async (route: Route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;

    if (req.method() === "POST" && path.endsWith("/action")) {
      const m = path.match(/\/api\/invoices\/([^/]+)\/action$/);
      actioned.docId = m ? decodeURIComponent(m[1]) : null;
      actioned.body = req.postDataJSON?.() ?? null;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "success",
          message: "ok",
          doc_id: actioned.docId,
          folder_name: actioned.docId,
          new_status: "review",
        }),
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

    // Lista: GET /api/invoices (con o sin query).
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        invoices: RAW_ORDER.map(listItem),
        total: RAW_ORDER.length,
      }),
    });
  });

  await page.goto("/");

  // El reviewer aparece cuando el primer poll de /batch llena visibleSummaries.
  const dotDisplayed = page.getByRole("button", { name: `Ir a factura ${DOC_DISPLAYED}` });
  const dotWrong = page.getByRole("button", { name: `Ir a factura ${DOC_WRONG}` });
  await expect(dotWrong).toBeVisible({ timeout: 30_000 });
  await expect(dotDisplayed).toBeVisible();

  // Navegar a la 2ª factura visible (F-LOTE-2).
  await dotDisplayed.click();

  // Confirmamos que es ESTA la que está en pantalla (cabecera muestra "#id").
  await expect(page.getByText(`#${DOC_DISPLAYED}`)).toBeVisible({ timeout: 15_000 });

  // Rechazar. rejection_count=0 ⇒ botón "Rechazar" ⇒ POST directo (sin diálogo).
  const rejectBtn = page.getByRole("button", { name: /^Rechazar$/ });
  await expect(rejectBtn).toBeEnabled({ timeout: 15_000 });
  await rejectBtn.click();

  // El sistema debió accionar EXACTAMENTE la factura en pantalla.
  await expect.poll(() => actioned.docId, { timeout: 15_000 }).toBe(DOC_DISPLAYED);
  expect(
    actioned.docId,
    `el reviewer rechazó '${actioned.docId}' pero en pantalla estaba '${DOC_DISPLAYED}'`,
  ).not.toBe(DOC_WRONG);
  expect((actioned.body as { action?: string } | null)?.action).toBe("reject");
});
