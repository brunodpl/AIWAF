/**
 * Spec 01 — Reproducción del bug "3 facturas → 1 procesada".
 *
 * Este test NO falla con expect. Su único objetivo es recolectar evidencia
 * sobre qué le pasa al PDF multi-factura al subirlo. Adjunta como artefactos:
 *   - Respuesta del upload
 *   - Snapshots del /api/pipeline/batch durante el run
 *   - Snapshot final de /api/invoices
 *   - Diagnóstico: ¿cuántas facturas detectó el splitter?
 *
 * El veredicto se imprime en el output del test. Lo lee Bruno después.
 */

import {
  test,
  expect,
  hardReset,
  uploadFile,
  runPipelineAndWait,
  fetchInvoices,
  fetchBatch,
  invoicesFromMultiPdf,
  attachJson,
  MULTI_INVOICE_PDF,
  MULTI_INVOICE_FILENAME,
} from "./fixtures";

test.describe.serial("Bug reproduction: 3 invoices → ?", () => {
  test("subir PDF de 3 facturas y diagnosticar cuántas procesa el pipeline", async ({
    request,
  }, testInfo) => {
    test.setTimeout(10 * 60_000); // 10 min — Gemini real

    // ─── 1. Limpieza dura ────────────────────────────────────────────────
    await hardReset(request);

    // ─── 2. Subir el PDF al libro "gastos" (compras) ─────────────────────
    const uploadResult = await uploadFile(
      request,
      "gastos",
      MULTI_INVOICE_PDF,
      MULTI_INVOICE_FILENAME
    );
    await attachJson(testInfo, "01-upload-result.json", uploadResult);
    console.log(`[01] Upload status: ${uploadResult.status}`);
    expect(uploadResult.status, "upload should succeed").toBeLessThan(400);

    // ─── 3. Snapshot pre-run para detectar transición real ─────────────
    const preStatusRes = await request.get(`/api/pipeline/status`);
    const preStatus = preStatusRes.ok() ? await preStatusRes.json() : null;
    const sentinelStartedAt = (preStatus?.started_at as string | null) ?? null;

    const startRes = await request.post(`/api/pipeline/run`);
    await attachJson(testInfo, "02-pipeline-start.json", {
      status: startRes.status(),
      body: await startRes.json().catch(() => null),
      sentinel_started_at: sentinelStartedAt,
    });

    // Poll batch + status cada 4s. Mantener snapshots para diagnóstico.
    const batchSnapshots: unknown[] = [];
    const startedAt = Date.now();
    const maxWaitMs = 7 * 60_000;
    // Espera grace para que el thread arranque y started_at se actualice.
    await new Promise((r) => setTimeout(r, 2_000));
    while (Date.now() - startedAt < maxWaitMs) {
      const statusRes = await request.get(`/api/pipeline/status`);
      const statusBody = statusRes.ok() ? await statusRes.json() : null;
      const batch = await fetchBatch(request);
      batchSnapshots.push({
        ts: new Date().toISOString(),
        status: statusBody,
        batch,
      });
      const st = String(statusBody?.status ?? "");
      const sa = (statusBody?.started_at as string | null) ?? null;
      const ca = (statusBody?.completed_at as string | null) ?? null;
      // Done = no running y vemos completed_at del NUEVO run.
      const isRunning = st === "running" || (sa != null && ca == null);
      const newRunCompleted =
        !isRunning &&
        ca != null &&
        (sentinelStartedAt == null || (sa != null && sa > sentinelStartedAt));
      if (newRunCompleted) break;
      await new Promise((r) => setTimeout(r, 4_000));
    }
    await attachJson(testInfo, "03-batch-snapshots.json", batchSnapshots);

    // ─── 5. Snapshot final de /api/invoices ──────────────────────────────
    const allInvoices = await fetchInvoices(request, true);
    const mine = invoicesFromMultiPdf(allInvoices);
    await attachJson(testInfo, "04-invoices-final.json", {
      total: allInvoices.length,
      from_multi_pdf: mine.length,
      doc_ids: mine.map((i) => i.id),
      invoices: mine,
    });

    // ─── 6. Diagnóstico verboso al stdout ────────────────────────────────
    const docIds = mine.map((i) => i.id);
    console.log(`[01] === DIAGNÓSTICO ===`);
    console.log(`[01] Facturas totales en /api/invoices: ${allInvoices.length}`);
    console.log(`[01] Facturas derivadas del PDF subido:  ${mine.length}`);
    console.log(`[01] doc_ids: ${JSON.stringify(docIds)}`);

    if (mine.length === 3) {
      console.log("[01] ✓ Splitter funcionó: 3 facturas extraídas.");
    } else if (mine.length === 1) {
      console.log(
        "[01] ✗ Bug reproducido: solo 1 factura procesada. El splitter " +
          "probablemente no se ejecutó o devolvió single_factura. " +
          "Revisar libros/logs/audit/splits_*.jsonl y libros/logs/pipeline.jsonl."
      );
    } else {
      console.log(
        `[01] ⚠ Resultado inesperado: ${mine.length} facturas. Caso edge.`
      );
    }

    // ─── 7. Aserción suave: solo verifica que llegamos al final ─────────
    // Es una reproducción diagnóstica, no un gate. Lo importante son los
    // artefactos adjuntos.
    expect(allInvoices.length).toBeGreaterThanOrEqual(0);
  });
});
