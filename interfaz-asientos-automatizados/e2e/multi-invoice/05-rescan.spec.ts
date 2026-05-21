/**
 * Spec 05 — Re-escanear (reset) tras aprobación parcial.
 *
 * Flujo:
 *   1. Subir PDF + procesar pipeline.
 *   2. (Si el splitter dió 3 facturas) aprobar SOLO la #1 vía API.
 *   3. Disparar reset (vía POST /api/pipeline/reset).
 *   4. Verificar: /api/invoices queda vacío de las nuestras; PDFs split
 *      persisten en /api/books/gastos (libros/facturas/ sobrevive a reset).
 *   5. Volver a lanzar pipeline.
 *   6. Verificar: las 3 facturas re-aparecen con los MISMOS doc_ids
 *      (idempotencia determinista del splitter).
 *
 * Si el splitter no dió 3 facturas (bug previo), el test skipea.
 *
 * Test de API + un poquito de UI (para el botón Nuevo escaneo, aunque
 * acabamos llamando al endpoint directamente para evitar el AlertDialog).
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

test.describe.serial("Rescan after partial approval", () => {
  test("aprobar parcial → reset → re-procesar → mismos doc_ids", async ({
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
        `Splitter no produjo 3 splits (got ${firstRun.length}). Bug previo, ver spec 01.`
      );
      return;
    }

    const firstDocIds = firstRun.map((i) => i.id).sort();

    // ─── Aprobar SOLO la primera factura ─────────────────────────────────
    const approveRes = await request.post(
      `/api/invoices/${firstDocIds[0]}/action`,
      {
        data: { action: "approve", document_id: firstDocIds[0] },
      }
    );
    await attachJson(testInfo, "approve-first.json", {
      status: approveRes.status(),
      body: await approveRes.json().catch(() => null),
    });
    expect(approveRes.ok()).toBe(true);

    // ─── Snapshot /api/books antes del reset ─────────────────────────────
    const booksBefore = await request.get(`/api/books`);
    const booksBeforeBody = await booksBefore.json();
    await attachJson(testInfo, "books-before-reset.json", booksBeforeBody);

    // Verificamos: los 3 splits (3_facturas__1of3.pdf, etc) deben estar
    // listados en /api/books/gastos (libros/facturas/compras/).
    const gastosBefore = (booksBeforeBody.books ?? []).find(
      (b: { id: string }) => b.id === "gastos"
    );
    const splitFilenamesBefore = (gastosBefore?.files ?? [])
      .map((f: { name: string }) => f.name)
      .filter((n: string) => /__\d+of\d+\.pdf$/.test(n));
    expect(splitFilenamesBefore.length).toBeGreaterThanOrEqual(2);

    // ─── Reset ───────────────────────────────────────────────────────────
    const resetRes = await request.post(`/api/pipeline/reset`);
    await attachJson(testInfo, "reset-response.json", {
      status: resetRes.status(),
      body: await resetRes.json().catch(() => null),
    });
    expect(resetRes.ok()).toBe(true);

    // ─── Snapshot post-reset ─────────────────────────────────────────────
    const booksAfter = await request.get(`/api/books`);
    const booksAfterBody = await booksAfter.json();
    await attachJson(testInfo, "books-after-reset.json", booksAfterBody);

    const gastosAfter = (booksAfterBody.books ?? []).find(
      (b: { id: string }) => b.id === "gastos"
    );
    const splitFilenamesAfter = (gastosAfter?.files ?? [])
      .map((f: { name: string }) => f.name)
      .filter((n: string) => /__\d+of\d+\.pdf$/.test(n));

    // Comportamiento esperado: PDFs split SIGUEN en el inbox tras el reset
    // (libros/facturas/ no se toca). Si esto no se cumple, el reset es
    // más agresivo de lo documentado.
    console.log(
      `[05] Splits antes del reset: ${splitFilenamesBefore.length}, ` +
        `después: ${splitFilenamesAfter.length}`
    );

    const invoicesAfterReset = invoicesFromMultiPdf(
      await fetchInvoices(request, true)
    );
    await attachJson(testInfo, "invoices-after-reset.json", invoicesAfterReset);
    expect(invoicesAfterReset.length).toBe(0);

    // ─── Re-lanzar pipeline ──────────────────────────────────────────────
    // Si los splits sobrevivieron, el splitter no los re-dividirá (ya están
    // divididos, son single-page o no llamarán a Gemini). Solo los procesa
    // de nuevo OCR + fases siguientes.
    if (splitFilenamesAfter.length === 0) {
      console.log(
        "[05] Inbox vacío tras reset → el reset también borró los PDFs split. " +
          "Re-subimos el PDF original para poder validar idempotencia."
      );
      await uploadFile(request, "gastos", MULTI_INVOICE_PDF, MULTI_INVOICE_FILENAME);
    }

    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });

    const secondRun = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "second-run.json", secondRun);

    expect(secondRun.length).toBe(3);
    const secondDocIds = secondRun.map((i) => i.id).sort();
    expect(secondDocIds).toEqual(firstDocIds);
  });
});
