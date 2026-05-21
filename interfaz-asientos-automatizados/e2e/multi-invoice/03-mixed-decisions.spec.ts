/**
 * Spec 03 — Decisiones mixtas: editar campo, aprobar, rechazar.
 *
 * Para las 3 facturas del PDF:
 *   - Factura #1: editar `nombre_entidad` a un valor conocido, luego Aprobar.
 *   - Factura #2: Rechazar.
 *   - Factura #3: Aprobar tal cual.
 *
 * Verificaciones duras:
 *   - El payload del POST /api/pipeline/confirm lleva SOLO 2 doc_ids.
 *   - La factura #1 lleva `nombre_entidad: <EDITADO>` en `campos_finales`.
 *   - El doc_id de la factura #2 NO está en el payload.
 *
 * Si /api/invoices < 3 tras el splitter, skipeamos (depende del bug previo).
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

const EDITED_VALUE = "EDITADO_E2E_TEST";

test.describe.serial("Mixed decisions", () => {
  test("editar+aprobar / rechazar / aprobar — verificar export", async ({
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
        `Esperábamos 3 facturas; encontradas ${mineBefore.length}. Bug previo, ver spec 01.`
      );
      return;
    }

    const orderedDocIds = [...mineBefore]
      .map((i) => i.id)
      .sort((a, b) => a.localeCompare(b));
    const docIdFactura1 = orderedDocIds[0];
    const docIdFactura2 = orderedDocIds[1];

    await page.goto("/");
    const escanearBtn = page.getByRole("button", { name: /escanear .* factura/i });
    await expect(escanearBtn).toBeVisible({ timeout: 30_000 });
    await escanearBtn.click();

    // Esperar a que el reviewer esté listo (puede haber re-run pipeline)
    await expect(
      page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i })
    ).toBeVisible({ timeout: 3 * 60_000 });

    // Espía del confirm: payload (request) + response (status + body)
    let confirmPayload: unknown = null;
    let confirmResponse: { status: number; body: unknown } | null = null;
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
    page.on("response", async (res) => {
      if (
        res.url().endsWith("/api/pipeline/confirm") &&
        res.request().method() === "POST"
      ) {
        try {
          confirmResponse = { status: res.status(), body: await res.json() };
        } catch {
          confirmResponse = { status: res.status(), body: await res.text().catch(() => null) };
        }
      }
    });

    // ─── Factura #1: editar nombre_entidad + Aprobar ─────────────────────
    const dot1 = page.getByRole("button", { name: /Ir a factura/ }).nth(0);
    await dot1.click();
    await page.waitForTimeout(800);

    // El input de nombre_entidad tiene id="nombre_entidad" (mapeo del
    // transformToInvoice en lib/api.ts).
    const nombreInput = page.locator("input#nombre_entidad");
    await expect(nombreInput).toBeVisible({ timeout: 30_000 });
    await nombreInput.fill(EDITED_VALUE);
    // Trigger blur para que onBlur del React handler dispare cambios.
    await nombreInput.blur();

    await page.getByRole("button", { name: /^Aprobar$/i }).click();
    await expect
      .poll(
        async () =>
          (
            await page
              .getByRole("button", { name: /^(Aprobar|Aprobada)$/i })
              .textContent()
          )?.trim() ?? "",
        { timeout: 30_000 }
      )
      .toMatch(/Aprobada/i);

    // ─── Factura #2: Rechazar ────────────────────────────────────────────
    const dot2 = page.getByRole("button", { name: /Ir a factura/ }).nth(1);
    await dot2.click();
    await page.waitForTimeout(800);
    await page.getByRole("button", { name: /^Rechazar$/i }).click();
    await expect
      .poll(
        async () =>
          (
            await page
              .getByRole("button", { name: /^(Rechazar|Rechazada)$/i })
              .textContent()
          )?.trim() ?? "",
        { timeout: 30_000 }
      )
      .toMatch(/Rechazada/i);

    // ─── Factura #3: Aprobar ─────────────────────────────────────────────
    const dot3 = page.getByRole("button", { name: /Ir a factura/ }).nth(2);
    await dot3.click();
    await page.waitForTimeout(800);
    await page.getByRole("button", { name: /^Aprobar$/i }).click();
    await expect
      .poll(
        async () =>
          (
            await page
              .getByRole("button", { name: /^(Aprobar|Aprobada)$/i })
              .textContent()
          )?.trim() ?? "",
        { timeout: 30_000 }
      )
      .toMatch(/Aprobada/i);

    // ─── Generar Asientos + Confirmar ────────────────────────────────────
    const generar = page.getByRole("button", { name: /Generar Asientos/i });
    await expect(generar).toBeEnabled({ timeout: 30_000 });
    await generar.click();

    const confirmar = page.getByRole("button", {
      name: /Confirmar y seguir escaneando/i,
    });
    await expect(confirmar).toBeVisible({ timeout: 30_000 });
    await confirmar.click();

    const dialogConfirm = page
      .getByRole("button", { name: /confirmar|aceptar|continuar/i })
      .filter({ hasNotText: /seguir escaneando/i })
      .first();
    if (await dialogConfirm.isVisible().catch(() => false)) {
      await dialogConfirm.click();
    }

    // ─── Esperar a que el confirm haya llegado al backend ────────────────
    await expect.poll(() => confirmPayload, { timeout: 30_000 }).not.toBeNull();
    await attachJson(testInfo, "confirm-payload.json", confirmPayload);
    await expect.poll(() => confirmResponse, { timeout: 30_000 }).not.toBeNull();
    await attachJson(testInfo, "confirm-response.json", confirmResponse);

    const payload = confirmPayload as {
      doc_ids: string[];
      asientos: Record<
        string,
        { campos_finales?: Record<string, unknown> }
      >;
    };

    // ─── Aserciones duras ────────────────────────────────────────────────
    expect(payload.doc_ids).toHaveLength(2);
    expect(payload.doc_ids).not.toContain(docIdFactura2);

    const asiento1 = payload.asientos?.[docIdFactura1];
    expect(asiento1, `falta asiento para ${docIdFactura1}`).toBeTruthy();
    const campos = asiento1?.campos_finales || {};
    // El backend puede llevar el valor como string plano o como objeto
    // {valor, decision}. Aceptamos ambos.
    const nombreFinal = (() => {
      const v = (campos as Record<string, unknown>).nombre_entidad;
      if (typeof v === "string") return v;
      if (v && typeof v === "object" && "valor" in (v as object)) {
        return String((v as { valor: unknown }).valor);
      }
      return "";
    })();
    expect(nombreFinal).toContain(EDITED_VALUE);

    // ─── Verificar estado backend ────────────────────────────────────────
    const final = invoicesFromMultiPdf(await fetchInvoices(request, true));
    await attachJson(testInfo, "invoices-after-confirm.json", final);

    const confirmados = final.filter((i) => i.status === "confirmed");
    expect(confirmados).toHaveLength(2);
  });
});
