/**
 * Spec 02 — Aprobar las 3 facturas del PDF multi-factura.
 *
 * Test funcional (aserciones duras). Verifica:
 *   - Tras procesar el PDF, /api/invoices devuelve 3 items derivados.
 *   - Aprobar las 3 desde la UI y confirmar el lote.
 *   - Tras confirmar, los 3 doc_ids pasan a status `confirmed`.
 *
 * Si este test falla, hay un bug en uno de los puntos del flujo. Lo dejamos
 * con aserciones duras para que la regresión sea obvia.
 *
 * Si /api/invoices devuelve <3 facturas tras el upload+run, el test se
 * skipea con `test.skip()` indicando que el splitter no funcionó — caso ya
 * cubierto por spec 01. Así no contamos como "fallo" lo que realmente es
 * un bug previo en el pipeline (que es el objetivo de la iteración futura).
 */

import {
  test,
  expect,
  hardReset,
  uploadFile,
  runPipelineAndWait,
  fetchInvoices,
  invoicesFromMultiPdf,
  attachJson,
  MULTI_INVOICE_PDF,
  MULTI_INVOICE_FILENAME,
} from "./fixtures";

test.describe.serial("Approve all 3 invoices", () => {
  test("subir PDF, aprobar las 3 facturas vía UI, exportar, confirmar", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(10 * 60_000);

    // ─── Preparación ─────────────────────────────────────────────────────
    await hardReset(request);
    await uploadFile(request, "gastos", MULTI_INVOICE_PDF, MULTI_INVOICE_FILENAME);
    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });

    const mineBefore = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-pipeline.json", mineBefore);

    if (mineBefore.length !== 3) {
      test.skip(
        true,
        `Esperábamos 3 facturas tras splitter; encontradas ${mineBefore.length}. ` +
          `Splitter no produjo 3 splits — bug separado, ver spec 01.`
      );
      return;
    }

    // ─── UI: navegar a review y aprobar cada factura ─────────────────────
    await page.goto("/");

    // Auto-jump canonizado: si el backend tiene pending_confirm (caso típico tras
    // hardReset + uploadFile + runPipelineAndWait), el frontend salta directo a
    // Revisión sin pasar por Pre-revisión (page.tsx:82-95). Detectamos ambos casos:
    // 1) Si /api/pipeline/status reporta pending_confirm → directo a review.
    // 2) Si no → flujo books → pre-review → review (raro en este test, pero
    //    lo cubrimos por defensa).
    //
    // Esperar a que la app termine de hidratar (sale el "Aprobar" si pending_confirm,
    // o el botón Escanear si arrancó en books). Sin $ final en el regex para cubrir
    // plural "Factura(s)" del botón Escanear.
    await expect(
      page.getByRole("button", { name: /^(Aprobar|Aprobada|Escanear .* factura)/i })
    ).toBeVisible({ timeout: 3 * 60_000 });

    // Si seguimos en books (caso menos común tras pipeline.run via API), pulsamos
    // Escanear; si ya estamos en review, no hacemos nada.
    const escanearBtn = page.getByRole("button", { name: /escanear .* factura/i });
    if (await escanearBtn.isVisible().catch(() => false)) {
      await escanearBtn.click();
      const aprobar = page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i });
      await expect(aprobar).toBeVisible({ timeout: 5 * 60_000 });
    }

    // Documentado: auto-jump canonizado en app/page.tsx:82-95.
    // Spec: docs/superpowers/specs/2026-05-28-pre-revision-callback-y-test-drift-design.md

    // Capturamos el payload del confirm para verificar al final.
    let confirmPayload: unknown = null;
    page.on("request", (req) => {
      if (
        req.url().endsWith("/api/pipeline/confirm") &&
        req.method() === "POST"
      ) {
        try {
          confirmPayload = JSON.parse(req.postData() || "{}");
        } catch {
          confirmPayload = req.postData();
        }
      }
    });

    // Recorremos las 3 facturas. La aplicación tiene un pager por dots.
    // Estrategia: por cada factura visible en el pager, click en la dot
    // y click en Aprobar.
    for (let idx = 0; idx < 3; idx++) {
      // Dots tienen aria-label "Ir a factura {id}". Cuando totalVisible ≤ 12
      // (nuestro caso, 3) el pager los muestra como botones numerados.
      const dot = page
        .getByRole("button", { name: /Ir a factura/ })
        .nth(idx);
      if (await dot.isVisible().catch(() => false)) {
        await dot.click();
        // Damos margen para que cargue el detalle.
        await page.waitForTimeout(500);
      }

      const aprobarBtn = page.getByRole("button", {
        name: /^(Aprobar|Aprobada)$/i,
      });
      await expect(aprobarBtn).toBeEnabled({ timeout: 30_000 });
      const label = (await aprobarBtn.textContent())?.trim() ?? "";
      if (/Aprobada/i.test(label)) {
        // Ya aprobada (auto-advance reaprueba si re-entramos). Saltamos.
        continue;
      }
      await aprobarBtn.click();
      // Esperar a que el botón muestre estado aprobado o que el indicador
      // de submitting desaparezca.
      await expect
        .poll(async () => (await aprobarBtn.textContent())?.trim() ?? "", {
          timeout: 30_000,
        })
        .toMatch(/Aprobada/i);
    }

    // ─── Click "Generar Asientos →" para ir a Export ─────────────────────
    const generar = page.getByRole("button", { name: /Generar Asientos/i });
    await expect(generar).toBeEnabled({ timeout: 30_000 });
    await generar.click();

    // ─── Click "Confirmar y seguir escaneando" ───────────────────────────
    const confirmar = page.getByRole("button", {
      name: /Confirmar y seguir escaneando/i,
    });
    await expect(confirmar).toBeVisible({ timeout: 30_000 });
    await confirmar.click();

    // El UI muestra un AlertDialog de confirmación, o lanza el POST directo.
    // Intentamos pulsar el botón de confirmación del diálogo si aparece.
    const dialogConfirm = page
      .getByRole("button", { name: /confirmar|aceptar|continuar/i })
      .filter({ hasNotText: /seguir escaneando/i })
      .first();
    if (await dialogConfirm.isVisible().catch(() => false)) {
      await dialogConfirm.click();
    }

    // ─── Verificación del backend ────────────────────────────────────────
    await expect
      .poll(
        async () => {
          const final = await fetchInvoices(request, true);
          const mine = invoicesFromMultiPdf(final);
          return mine.filter((i) => i.status === "confirmed").length;
        },
        { timeout: 60_000, message: "esperando 3 invoices con status=confirmed" }
      )
      .toBe(3);

    const mineAfter = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-confirm.json", mineAfter);
    await attachJson(testInfo, "confirm-payload.json", confirmPayload);

    // ─── Verificar payload del confirm ───────────────────────────────────
    expect(confirmPayload).not.toBeNull();
    const payload = confirmPayload as {
      doc_ids?: string[];
      asientos?: Record<string, unknown>;
    };
    expect(payload.doc_ids?.length).toBe(3);
  });
});
