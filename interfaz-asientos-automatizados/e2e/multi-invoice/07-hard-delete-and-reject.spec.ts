/**
 * Spec 07 — Hard delete en Pre-revisión + rechazo en dos pasos.
 *
 * Cubre los comportamientos nuevos del refactor (Tasks 1-8):
 *
 *  1. Hard delete en Pre-revisión: el botón "Eliminar" (solo en archivos
 *     `blocked`/`pre_scan_failed`) abre un AlertDialog "Eliminar
 *     definitivamente" y, al confirmar, borra el PDF + carpeta de asiento
 *     (borrado duro via DELETE /api/books/{bookId}/files/{filename}).
 *     ⚠ Requiere forzar un fallo de pre-scan (Gemini Vision caído) que NO se
 *     puede provocar E2E con llamadas reales → marcado `test.fixme` con notas
 *     de setup manual. La maquinaria de borrado SÍ se valida en el test 3.
 *
 *  2. Primer rechazo en el reviewer: rechazar una factura recién escaneada →
 *     el sidecar transiciona a `review`, `rejection_count === 1`, y aparece el
 *     toast "Volverá a aparecer en el próximo escaneo".
 *
 *  3. Segundo rechazo = hard delete: una factura que ya tiene
 *     `rejection_count >= 1` muestra el botón "Eliminar definitivamente";
 *     al pulsarlo se abre el AlertDialog y, al confirmar, se borra del todo
 *     (su doc_id desaparece de /api/invoices).
 *
 * Aserciones de backend: vía GET /api/invoices (como specs 03/05), leyendo
 * `status` y `rejection_count` que el backend expone por factura
 * (ver src/api/main.py → record["rejection_count"]).
 *
 * Si el splitter no produce facturas (bug previo, ver spec 01), el test
 * skipea — igual que 02/03/05.
 *
 * Coste: ~1 ciclo de pipeline por test que llega a review (Gemini real). El
 * test 3 corre el pipeline dos veces (escaneo inicial + re-escaneo tras el
 * primer rechazo para el carry-over).
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
  type InvoiceListItem,
} from "./fixtures";

/**
 * `rejection_count` lo expone /api/invoices pero no está en el tipo base de
 * fixtures (ver src/api/main.py L452). Lo añadimos localmente para esta suite.
 */
type InvoiceWithReject = InvoiceListItem & { rejection_count?: number };

test.describe.serial("Hard delete + rechazo en dos pasos", () => {
  // ───────────────────────────────────────────────────────────────────────
  // Test 1 — Hard delete en Pre-revisión sobre un archivo bloqueado.
  //
  // El botón "Eliminar" SOLO se renderiza para archivos con
  // `pre_scan.status === "failed"` (blocked). Forzar ese estado exige que
  // Gemini Vision falle durante el /upload, cosa que no se puede provocar de
  // forma determinista con llamadas reales (la suite no mockea — ver README).
  //
  // Por eso este test queda como `fixme`: documenta el flujo y las
  // aserciones esperadas para cuando un humano pueda forzar el bloqueo.
  // El MECANISMO de borrado duro (deleteBookFile → DELETE /api/books/…/files)
  // queda cubierto por el test 3, que ejecuta exactamente la misma llamada.
  // ───────────────────────────────────────────────────────────────────────
  test.fixme(
    "Pre-revisión: 'Eliminar' en archivo bloqueado → AlertDialog → hard delete",
    async ({ page, request }, testInfo) => {
      test.setTimeout(10 * 60_000);

      // SETUP MANUAL NECESARIO (no driveable con Gemini real):
      //   - Forzar `pre_scan.status === "failed"` para el PDF subido. Opciones:
      //     a) Variable de entorno / flag de backend que simule fallo de
      //        pre-scan, o b) subir un PDF corrupto que Gemini Vision rechace.
      //   - Sin un archivo `blocked`, el botón "Eliminar" no se renderiza.
      await hardReset(request);
      await page.goto("/");

      // (Tras forzar el bloqueo) llegaríamos al stage Pre-revisión con el
      // archivo en estado bloqueado y los botones de recovery visibles.
      await expect(
        page.getByRole("heading", { name: /Pre-revisión/i }),
      ).toBeVisible({ timeout: 90_000 });

      // El botón "Eliminar" (ghost, rojo) solo está en filas bloqueadas.
      const eliminarBtn = page.getByRole("button", { name: /^Eliminar$/i }).first();
      await expect(eliminarBtn).toBeVisible();
      await eliminarBtn.click();

      // AlertDialog "Eliminar definitivamente".
      const dialog = page.getByRole("alertdialog");
      await expect(
        dialog.getByRole("heading", { name: /Eliminar definitivamente/i }),
      ).toBeVisible();

      // Confirmar con el botón de acción "Eliminar definitivamente".
      await dialog
        .getByRole("button", { name: /Eliminar definitivamente/i })
        .click();

      // Toast de confirmación + el archivo desaparece de la lista.
      await expect(page.getByText(/Archivo eliminado/i)).toBeVisible();
      await expect(page.getByText(MULTI_INVOICE_FILENAME)).toHaveCount(0);

      // Backend: el PDF y su carpeta de asiento ya no existen.
      const invoices = invoicesFromMultiPdf(await fetchInvoices(request, true));
      await attachJson(testInfo, "invoices-after-hard-delete.json", invoices);
      expect(invoices).toHaveLength(0);
    },
  );

  // ───────────────────────────────────────────────────────────────────────
  // Test 2 — Primer rechazo: review + rejection_count === 1 + toast.
  // ───────────────────────────────────────────────────────────────────────
  test("Primer rechazo → status=review, rejection_count=1, toast de re-aparición", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(10 * 60_000);

    await hardReset(request);
    await uploadFile(request, "gastos", MULTI_INVOICE_PDF, MULTI_INVOICE_FILENAME);
    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });

    const mineBefore = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-pipeline.json", mineBefore);

    if (mineBefore.length !== 3) {
      test.skip(
        true,
        `Esperábamos 3 facturas; encontradas ${mineBefore.length}. Bug previo, ver spec 01.`,
      );
      return;
    }

    // doc_id de la factura que vamos a rechazar (la primera por orden estable).
    const orderedDocIds = [...mineBefore]
      .map((i) => i.id)
      .sort((a, b) => a.localeCompare(b));
    const docIdRechazada = orderedDocIds[0];

    // ─── Abrir el reviewer ───────────────────────────────────────────────
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
      page.getByRole("button", { name: /^(Aprobar|Aprobada|Escanear .* factura)/i }),
    ).toBeVisible({ timeout: 3 * 60_000 });

    // Si seguimos en books (caso menos común tras pipeline.run via API), pulsamos
    // Escanear; si ya estamos en review, no hacemos nada.
    const escanearBtn = page.getByRole("button", { name: /escanear .* factura/i });
    if (await escanearBtn.isVisible().catch(() => false)) {
      await escanearBtn.click();
      await expect(
        page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i }),
      ).toBeVisible({ timeout: 5 * 60_000 });
    }

    // Documentado: auto-jump canonizado en app/page.tsx:82-95.
    // Spec: docs/superpowers/specs/2026-05-28-pre-revision-callback-y-test-drift-design.md

    // ─── Navegar a la factura #1 y rechazarla ────────────────────────────
    const dot1 = page.getByRole("button", { name: /Ir a factura/ }).nth(0);
    await dot1.click();
    await page.waitForTimeout(800);

    // Recién escaneada → rejection_count=0 → el botón dice "Rechazar".
    const rechazarBtn = page.getByRole("button", { name: /^Rechazar$/i });
    await expect(rechazarBtn).toBeVisible({ timeout: 30_000 });
    await rechazarBtn.click();

    // ─── Toast de carry-over ─────────────────────────────────────────────
    await expect(
      page.getByText(/Volverá a aparecer en el próximo escaneo/i),
    ).toBeVisible({ timeout: 30_000 });

    // El botón pasa a "Rechazada" (estado UI tras el rechazo).
    await expect
      .poll(
        async () =>
          (
            await page
              .getByRole("button", { name: /^(Rechazar|Rechazada)$/i })
              .first()
              .textContent()
          )?.trim() ?? "",
        { timeout: 30_000 },
      )
      .toMatch(/Rechazada/i);

    // ─── Aserciones de backend: status=review + rejection_count=1 ────────
    await expect
      .poll(
        async () => {
          const inv = (
            invoicesFromMultiPdf(
              await fetchInvoices(request, true),
            ) as InvoiceWithReject[]
          ).find((i) => i.id === docIdRechazada);
          return inv?.status ?? null;
        },
        { timeout: 30_000 },
      )
      .toBe("review");

    const finalMine = invoicesFromMultiPdf(
      await fetchInvoices(request, true),
    ) as InvoiceWithReject[];
    await attachJson(testInfo, "invoices-after-reject.json", finalMine);

    const rechazada = finalMine.find((i) => i.id === docIdRechazada);
    expect(rechazada, `falta la factura rechazada ${docIdRechazada}`).toBeTruthy();
    expect(rechazada?.status).toBe("review");
    expect(rechazada?.rejection_count).toBe(1);
  });

  // ───────────────────────────────────────────────────────────────────────
  // Test 3 — Segundo rechazo = hard delete.
  //
  // Flujo:
  //   1. Subir + procesar → 3 facturas en review.
  //   2. Rechazar #1 vía API → rejection_count=1, status=review.
  //   3. Re-lanzar pipeline (carry-over): la factura rechazada re-aparece.
  //   4. En el reviewer, su botón ahora dice "Eliminar definitivamente".
  //   5. Click → AlertDialog "Eliminar definitivamente" → confirmar.
  //   6. Backend: el doc_id desaparece de /api/invoices (carpeta borrada).
  // ───────────────────────────────────────────────────────────────────────
  test("Segundo rechazo: botón 'Eliminar definitivamente' → AlertDialog → hard delete", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(15 * 60_000);

    await hardReset(request);
    await uploadFile(request, "gastos", MULTI_INVOICE_PDF, MULTI_INVOICE_FILENAME);
    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });

    const firstRun = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "first-run.json", firstRun);

    if (firstRun.length !== 3) {
      test.skip(
        true,
        `Splitter no produjo 3 splits (got ${firstRun.length}). Bug previo, ver spec 01.`,
      );
      return;
    }

    const orderedDocIds = firstRun.map((i) => i.id).sort((a, b) => a.localeCompare(b));
    const docIdHardDelete = orderedDocIds[0];

    // ─── Primer rechazo vía API (deja rejection_count=1) ─────────────────
    const rejectRes = await request.post(
      `/api/invoices/${docIdHardDelete}/action`,
      { data: { action: "reject", document_id: docIdHardDelete } },
    );
    await attachJson(testInfo, "first-reject.json", {
      status: rejectRes.status(),
      body: await rejectRes.json().catch(() => null),
    });
    expect(rejectRes.ok()).toBe(true);

    // Confirmamos el estado intermedio: review + rejection_count=1.
    const afterReject = (
      invoicesFromMultiPdf(await fetchInvoices(request, true)) as InvoiceWithReject[]
    ).find((i) => i.id === docIdHardDelete);
    expect(afterReject?.status).toBe("review");
    expect(afterReject?.rejection_count).toBe(1);

    // ─── Re-lanzar pipeline para el carry-over ───────────────────────────
    // La factura rechazada vuelve a entrar en revisión en el nuevo escaneo.
    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });

    const secondRun = (
      invoicesFromMultiPdf(await fetchInvoices(request, true)) as InvoiceWithReject[]
    );
    await attachJson(testInfo, "second-run.json", secondRun);

    const carriedOver = secondRun.find((i) => i.id === docIdHardDelete);
    expect(
      carriedOver,
      `la factura rechazada ${docIdHardDelete} debería re-aparecer tras el carry-over`,
    ).toBeTruthy();
    // Tras el carry-over el contador sigue siendo >= 1 (el botón mostrará
    // "Eliminar definitivamente").
    expect((carriedOver?.rejection_count ?? 0)).toBeGreaterThanOrEqual(1);

    // ─── Abrir el reviewer y navegar a la factura carry-over ─────────────
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
      page.getByRole("button", { name: /^(Aprobar|Aprobada|Escanear .* factura)/i }),
    ).toBeVisible({ timeout: 3 * 60_000 });

    // Si seguimos en books (caso menos común tras pipeline.run via API), pulsamos
    // Escanear; si ya estamos en review, no hacemos nada.
    const escanearBtn = page.getByRole("button", { name: /escanear .* factura/i });
    if (await escanearBtn.isVisible().catch(() => false)) {
      await escanearBtn.click();
      await expect(
        page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i }),
      ).toBeVisible({ timeout: 5 * 60_000 });
    }

    // Documentado: auto-jump canonizado en app/page.tsx:82-95.
    // Spec: docs/superpowers/specs/2026-05-28-pre-revision-callback-y-test-drift-design.md

    // Recorremos los dots buscando la factura cuyo botón izquierdo muestra
    // "Eliminar definitivamente" (rejection_count >= 1). El orden del reviewer
    // puede no coincidir con el orden lexicográfico de doc_ids, así que
    // localizamos por el label del botón en vez de por índice.
    const dots = page.getByRole("button", { name: /Ir a factura/ });
    const dotCount = await dots.count();
    let found = false;
    for (let i = 0; i < dotCount; i++) {
      await dots.nth(i).click();
      await page.waitForTimeout(800);
      const leftBtnText =
        (
          await page
            .getByRole("button", {
              name: /^(Rechazar|Rechazada|Eliminar definitivamente)$/i,
            })
            .first()
            .textContent()
        )?.trim() ?? "";
      if (/Eliminar definitivamente/i.test(leftBtnText)) {
        found = true;
        break;
      }
    }
    expect(
      found,
      "ninguna factura del reviewer mostró el botón 'Eliminar definitivamente' " +
        "(rejection_count>=1 esperado tras el carry-over)",
    ).toBe(true);

    // ─── Click en "Eliminar definitivamente" → AlertDialog ───────────────
    await page
      .getByRole("button", { name: /^Eliminar definitivamente$/i })
      .first()
      .click();

    const dialog = page.getByRole("alertdialog");
    await expect(
      dialog.getByRole("heading", { name: /Eliminar definitivamente/i }),
    ).toBeVisible({ timeout: 30_000 });

    // Confirmar con el botón de acción del diálogo.
    await dialog
      .getByRole("button", { name: /^Eliminar definitivamente$/i })
      .click();

    // Toast de confirmación.
    await expect(
      page.getByText(/Factura eliminada definitivamente/i),
    ).toBeVisible({ timeout: 30_000 });

    // ─── Aserción dura de backend: el doc_id desaparece de /api/invoices ──
    await expect
      .poll(
        async () => {
          const mine = invoicesFromMultiPdf(await fetchInvoices(request, true));
          return mine.some((i) => i.id === docIdHardDelete);
        },
        { timeout: 30_000 },
      )
      .toBe(false);

    const finalMine = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-hard-delete.json", finalMine);
    expect(finalMine.some((i) => i.id === docIdHardDelete)).toBe(false);
  });
});
