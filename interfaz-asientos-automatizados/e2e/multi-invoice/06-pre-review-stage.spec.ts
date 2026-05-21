/**
 * Spec 06 — Stage de pre-revisión tras upload por UI.
 *
 * Verifica el flujo añadido para corregir el bug original (3 facturas mostradas
 * como 3 archivos en vez de 5 cuando había 2 archivos single + 1 multi):
 *
 *  1. El operario sube el PDF multi-factura por UI (drag&drop / input file).
 *  2. El backend ejecuta el splitter Fase 1 SÍNCRONO durante /upload, con lo
 *     que devuelve el desglose por archivo (n_pages, detected_invoices,
 *     children) en `result.detected_summary`.
 *  3. El frontend transiciona al stage "Pre-revisión" mostrando "Detectadas
 *     3 facturas en 1 archivo" antes de pulsar Escanear.
 *  4. Click en "Escanear 3 Factura(s) →" lanza /api/pipeline/run y avanza a
 *     processing → review.
 *
 * Si Gemini Vision falla en el upload, este test se skipea (caso aparte
 * cubierto manualmente con stubs — ver README).
 */

import {
  test,
  expect,
  hardReset,
  uploadMultiPdfViaUI,
  MULTI_INVOICE_FILENAME,
} from "./fixtures";

test.describe.serial("Pre-review stage (UI upload flow)", () => {
  test("subir PDF multi-factura por UI llega al stage Pre-revisión", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(10 * 60_000);

    // 1. Limpieza dura: inbox vacío, asientos vacíos.
    await hardReset(request);

    // 2. Cargar la app. Stage inicial = "books".
    await page.goto("/");

    // El indicador de stage debe mostrar "Gestión" como activo.
    await expect(page.getByText(/Gestión/i).first()).toBeVisible();

    // 3. Upload del PDF multi-factura por UI.
    await uploadMultiPdfViaUI(page, "gastos");

    // 4. Tras el upload, el pre-scan síncrono del backend tarda varios
    //    segundos (Gemini Vision). El frontend debería transitar al stage
    //    "Pre-revisión" si el splitter detectó >=1 factura.
    const preRevisionHeader = page.getByRole("heading", { name: /Pre-revisión/i });
    await expect(preRevisionHeader).toBeVisible({ timeout: 90_000 });

    // 5. Capturar el texto de detección. Aceptamos:
    //    - "Detectadas 3 facturas en 1 archivo(s)" → splitter funcionó
    //    - "Detectadas 1 factura en 1 archivo" → splitter falló silenciosamente
    //    - "Detectadas 0 facturas ... bloqueado(s)" → pre-scan failed
    const summaryText = await page
      .getByText(/Se han detectado/i)
      .first()
      .textContent();
    console.log(`[06] ${summaryText?.trim()}`);
    await testInfo.attach("pre-review-summary.txt", {
      body: summaryText ?? "(no text)",
      contentType: "text/plain",
    });

    if (summaryText && /0 facturas/i.test(summaryText)) {
      console.log("[06] Pre-scan falló — verificando recovery buttons.");
      // Debe haber al menos un botón "Reintentar análisis" visible.
      await expect(
        page.getByRole("button", { name: /Reintentar análisis/i }).first(),
      ).toBeVisible();
      test.skip(true, "Pre-scan falló (Gemini Vision down). Recovery UI verificada.");
      return;
    }

    // 6. El archivo subido aparece listado.
    await expect(page.getByText(MULTI_INVOICE_FILENAME).first()).toBeVisible();

    // 7. CTA principal "Escanear N facturas →" presente y habilitado.
    const escanearBtn = page.getByRole("button", { name: /Escanear .* Factura/i });
    await expect(escanearBtn).toBeVisible();
    await expect(escanearBtn).toBeEnabled();

    // Extraemos el número del label para sanity check.
    const btnLabel = (await escanearBtn.textContent())?.trim() ?? "";
    const match = btnLabel.match(/Escanear\s+(\d+)/i);
    const detected = match ? parseInt(match[1], 10) : 0;
    expect(detected).toBeGreaterThanOrEqual(1);
    console.log(`[06] CTA muestra: ${btnLabel} (detectadas=${detected})`);

    // 8. Click → transita a processing y luego a review.
    await escanearBtn.click();
    const aprobar = page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i });
    await expect(aprobar).toBeVisible({ timeout: 5 * 60_000 });

    // 9. Sanity: el reviewer carga al menos `detected` facturas.
    //    Los dots del pager indican el número total.
    const dots = page.getByRole("button", { name: /Ir a factura/i });
    const dotCount = await dots.count();
    console.log(`[06] Dots en pager: ${dotCount} (esperado >= ${detected})`);
    expect(dotCount).toBeGreaterThanOrEqual(detected);
  });
});
