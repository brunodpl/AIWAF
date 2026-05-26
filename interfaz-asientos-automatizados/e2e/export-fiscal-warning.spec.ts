import { test, expect, type Route } from "@playwright/test";
import { promises as fs } from "node:fs";

/**
 * E2E del stage de exportación (Error 1 — CSV emitidas "no se han encontrado
 * registros"). No necesita backend ni Gemini: siembra `approvedInvoices` +
 * stage="export" en localStorage y mockea `/api/**`. El restore de app/page.tsx
 * (offline-first) restaura el stage "export" con las facturas sembradas.
 *
 * Verifica el comportamiento "avisar, no fabricar":
 *  - Una emitida SIN desglose fiscal (fiscalLines vacío) AVISA antes de descargar
 *    (Intermega la rechazaría) y NO descarga en silencio.
 *  - Una emitida COMPLETA descarga un CSV con una fila real (BASE/IVA/CUOTA del
 *    desglose) y SIN fila fabricada de IVA cero.
 */

type Entry = [
  string,
  {
    formData: Record<string, string>;
    fiscalLines: Array<{ id: string; base: number; vatRate: number; vatAmount: number; total: number }>;
    libro: string;
    cuenta_contable: string;
  },
];

const FULL: Entry = [
  "doc-full",
  {
    formData: {
      nif_entidad: "B11111111",
      nombre_entidad: "ACME SL",
      nif_receptor: "B22222222",
      nombre_receptor: "CLIENTE FINAL SL",
      numero_factura: "F-100",
      fecha_expedicion: "2026-05-08",
      total_euros: "121",
      concepto: "Servicios",
      cuenta_contable: "700000",
    },
    fiscalLines: [{ id: "l1", base: 100, vatRate: 21, vatAmount: 21, total: 121 }],
    libro: "ingresos",
    cuenta_contable: "700000",
  },
];

const NO_FISCAL: Entry = [
  "doc-nofiscal",
  {
    formData: {
      nif_entidad: "B33333333",
      nombre_entidad: "BETA SL",
      nif_receptor: "B44444444",
      nombre_receptor: "OTRO CLIENTE SL",
      numero_factura: "F-200",
      fecha_expedicion: "2026-05-09",
      total_euros: "242",
      concepto: "Servicios",
      cuenta_contable: "700000",
    },
    fiscalLines: [],
    libro: "ingresos",
    cuenta_contable: "700000",
  },
];

async function seedAndMock(page: import("@playwright/test").Page, entries: Entry[]) {
  await page.addInitScript((data) => {
    localStorage.setItem("horeca_current_stage", "export");
    localStorage.setItem("horeca_approved_invoices", JSON.stringify(data));
  }, entries);

  // Catch-all primero (menor prioridad): devuelve {} para que fetchInvoices()
  // falle al hacer .map y el restore caiga en la rama offline-first.
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
}

test("emitida SIN desglose fiscal: avisa antes de descargar (no descarga en silencio)", async ({ page }) => {
  await seedAndMock(page, [NO_FISCAL]);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /Verificación de Asientos/i })).toBeVisible({ timeout: 30_000 });

  const downloadBtn = page.getByRole("button", { name: "Descargar", exact: true });
  await expect(downloadBtn).toBeVisible();
  await downloadBtn.click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(/desglose fiscal/i);
});

test("emitida COMPLETA: descarga CSV con fila real y sin fila fabricada de IVA cero", async ({ page }) => {
  await seedAndMock(page, [FULL]);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /Verificación de Asientos/i })).toBeVisible({ timeout: 30_000 });

  const downloadBtn = page.getByRole("button", { name: "Descargar", exact: true });
  await expect(downloadBtn).toBeVisible();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    downloadBtn.click(),
  ]);

  const filePath = await download.path();
  expect(filePath).toBeTruthy();
  const content = await fs.readFile(filePath!, "utf-8");

  // BOM + CRLF
  expect(content.charCodeAt(0)).toBe(0xfeff);
  const lines = content.split("\r\n").filter((l) => l.length > 0);
  expect(lines).toHaveLength(2); // cabecera + 1 fila real (no fabricada)

  const cells = lines[1].split(";");
  // FECHA;SERIE;Nº FACTURA;NOMBRE CLI-PRO;NIF CLI-PRO;DESCRIPCION;BASE;%IVA;CUOTA IVA;...;TOTAL FACTURA
  expect(cells[6]).toBe("100,00"); // BASE real
  expect(cells[7]).toBe("21"); // %IVA real (NO vacío de fabricación)
  expect(cells[8]).toBe("21,00"); // CUOTA real
  expect(cells[14]).toBe("121,00"); // TOTAL
});
