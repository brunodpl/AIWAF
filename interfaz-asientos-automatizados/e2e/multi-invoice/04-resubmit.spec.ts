/**
 * Spec 04 — Re-subir el mismo PDF: idempotencia + detección de duplicados.
 *
 * Casuísticas cubiertas:
 *   A. Tras procesar y confirmar el PDF, intentar re-subirlo al MISMO libro.
 *      Expectativa: rechazo del backend con código duplicate (HTTP 400/409).
 *
 *   B. Tras procesar el PDF en gastos, subirlo a OTRO libro (ventas).
 *      Sin expectativa rígida — solo documentamos qué hace el sistema:
 *      ¿se permite (sha igual pero libro distinto)?, ¿se rechaza?,
 *      ¿se permite pero acaba en duplicado fiscal en run posterior?
 *
 * Test puramente de API (request context). No abre browser. Más rápido y
 * resistente a cambios de UI.
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

test.describe.serial("Resubmit & duplicate detection", () => {
  test("re-subir mismo PDF tras confirmar al mismo libro → rechazo duplicado", async ({
    request,
  }, testInfo) => {
    test.setTimeout(10 * 60_000);

    await hardReset(request);

    // ── Fase A: upload + pipeline. Sin confirmación humana, dejamos los
    //    sidecars en estado uploaded/done/review según el pipeline. La
    //    detección de duplicados via sha256 funciona desde `uploaded` —
    //    no requiere `confirmed` (es a nivel de fichero físico).
    const first = await uploadFile(
      request,
      "gastos",
      MULTI_INVOICE_PDF,
      MULTI_INVOICE_FILENAME
    );
    await attachJson(testInfo, "first-upload.json", first);
    expect(first.status).toBeLessThan(400);

    // Procesamos para tener algo realista en estado.
    await runPipelineAndWait(request, { timeoutMs: 7 * 60_000 });
    const after = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-first-run.json", after);

    // ── Caso A: re-subir mismo PDF al mismo libro ─────────────────────────
    const sameBook = await uploadFile(
      request,
      "gastos",
      MULTI_INVOICE_PDF,
      MULTI_INVOICE_FILENAME
    );
    await attachJson(testInfo, "resubmit-same-book.json", sameBook);

    // Esperamos un rechazo. El contrato exacto del backend puede variar:
    //   - HTTP 400 con detail.code = "duplicate"
    //   - HTTP 200 pero con errors[] no vacío
    //   - HTTP 409
    // Aceptamos cualquiera de las dos primeras señales. Si ninguna se
    // cumple, el test falla — significa que el backend NO está detectando
    // el duplicado, lo cual es un bug serio.
    let duplicateDetected = false;
    const status = sameBook.status;
    const body = sameBook.body as
      | { uploaded?: string[]; errors?: { file: string; error: string }[]; detail?: unknown }
      | string
      | null;

    if (status === 400 || status === 409) {
      duplicateDetected = true;
    } else if (status === 200 && typeof body === "object" && body) {
      // Caso "OK pero con errores": uploaded vacío y errors menciona duplicado.
      const errs = (body as { errors?: { error: string }[] }).errors ?? [];
      const uploadedNames = (body as { uploaded?: string[] }).uploaded ?? [];
      const errStr = errs.map((e) => e.error).join("|").toLowerCase();
      if (
        uploadedNames.length === 0 ||
        errStr.includes("duplic") ||
        errStr.includes("already")
      ) {
        duplicateDetected = true;
      }
    }

    expect(
      duplicateDetected,
      `Esperábamos rechazo de duplicado al re-subir el mismo PDF al mismo libro. ` +
        `Got: status=${status} body=${JSON.stringify(body)}`
    ).toBe(true);

    // ── Caso B: subir mismo PDF a OTRO libro (documentar) ─────────────────
    const otherBook = await uploadFile(
      request,
      "ingresos",
      MULTI_INVOICE_PDF,
      MULTI_INVOICE_FILENAME
    );
    await attachJson(testInfo, "resubmit-other-book.json", otherBook);

    // No imponemos comportamiento esperado — solo lo dejamos documentado.
    // El operador humano (Bruno) decidirá si el backend debería:
    //   a) rechazar (mismo sha es siempre duplicado, da igual el libro), o
    //   b) permitir (sha es por libro), o
    //   c) permitir pero detectar duplicado fiscal posteriormente
    console.log(
      `[04] Otro libro: status=${otherBook.status} ` +
        `body=${JSON.stringify(otherBook.body).slice(0, 300)}`
    );
  });
});
