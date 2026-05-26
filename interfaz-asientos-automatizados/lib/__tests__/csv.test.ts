import { describe, it, expect } from "vitest";
import { intermegaRowsFor, generateIntermegaCSVsByCliente } from "../csv";
import type { IntermegaCsvFile } from "../csv";
import type { ApprovedInvoiceData } from "../types";

function makeInvoice(over: Partial<ApprovedInvoiceData> & { libro?: "ingresos" | "gastos" | "bienes" }): ApprovedInvoiceData {
  return {
    libro: over.libro,
    cuenta_contable: "",
    fiscalLines: over.fiscalLines ?? [
      { id: "l1", base: 100, vatRate: 21, vatAmount: 21, total: 121 },
    ],
    formData: {
      nif_entidad: "B11111111",
      nombre_entidad: "GESTORIA CLI SL",
      nif_receptor: "B22222222",
      nombre_receptor: "CLIENTE FINAL SL",
      nif_cliente: "B11111111",
      nombre_cliente: "GESTORIA CLI SL",
      numero_factura: "F-1",
      fecha_expedicion: "2026-05-08",
      total_euros: "121",
      concepto: "Servicios",
      ...(over.formData ?? {}),
    },
  };
}

describe("intermegaRowsFor — CLI-PRO is the counterpart, never our client", () => {
  it("emitida (libro=ingresos): CLI-PRO = receptor (cliente final)", () => {
    const rows = intermegaRowsFor(makeInvoice({ libro: "ingresos" }));
    expect(rows).toHaveLength(1);
    const cells = rows[0].split(";");
    // Header order: FECHA;SERIE;Nº FACTURA;NOMBRE CLI-PRO;NIF CLI-PRO;...
    expect(cells[3]).toBe("CLIENTE FINAL SL");
    expect(cells[4]).toBe("B22222222");
  });

  it("recibida (libro=gastos): CLI-PRO = entidad (proveedor)", () => {
    const rows = intermegaRowsFor(makeInvoice({
      libro: "gastos",
      formData: {
        nif_entidad: "B33333333",
        nombre_entidad: "PROVEEDOR SL",
        nif_receptor: "B11111111",
        nombre_receptor: "GESTORIA CLI SL",
      },
    }));
    const cells = rows[0].split(";");
    expect(cells[3]).toBe("PROVEEDOR SL");
    expect(cells[4]).toBe("B33333333");
  });

  it("bienes (libro=bienes): CLI-PRO = entidad (treated like recibida)", () => {
    const rows = intermegaRowsFor(makeInvoice({
      libro: "bienes",
      formData: {
        nif_entidad: "B44444444",
        nombre_entidad: "PROVEEDOR INVERSION SL",
        nif_receptor: "B11111111",
        nombre_receptor: "GESTORIA CLI SL",
      },
    }));
    const cells = rows[0].split(";");
    expect(cells[3]).toBe("PROVEEDOR INVERSION SL");
    expect(cells[4]).toBe("B44444444");
  });

  it("libro undefined: defaults to recibida → CLI-PRO = entidad", () => {
    const rows = intermegaRowsFor(makeInvoice({ libro: undefined,
      formData: { nif_entidad: "B55555555", nombre_entidad: "FALLBACK SL", nif_receptor: "B11111111", nombre_receptor: "GESTORIA CLI SL" }
    }));
    const cells = rows[0].split(";");
    expect(cells[3]).toBe("FALLBACK SL");
    expect(cells[4]).toBe("B55555555");
  });

  it("uppercases concepto in the DESCRIPCION column", () => {
    const rows = intermegaRowsFor(makeInvoice({
      libro: "ingresos",
      formData: { concepto: "alquiler_local" },
    }));
    const cells = rows[0].split(";");
    // Header order: ...;NIF CLI-PRO;DESCRIPCION;... → DESCRIPCION is index 5
    expect(cells[5]).toBe("ALQUILER_LOCAL");
  });

  it("nif_cliente / nombre_cliente are NOT used as CLI-PRO", () => {
    const rows = intermegaRowsFor(makeInvoice({
      libro: "gastos",
      formData: {
        nif_entidad: "B33333333",
        nombre_entidad: "PROVEEDOR SL",
        nif_cliente: "B99999999",
        nombre_cliente: "NEVER USE THIS",
      },
    }));
    const cells = rows[0].split(";");
    expect(cells[3]).not.toBe("NEVER USE THIS");
    expect(cells[4]).not.toBe("B99999999");
  });
});

function inv(libro: "ingresos" | "gastos" | "bienes", over: Record<string, string>): ApprovedInvoiceData {
  return {
    libro,
    cuenta_contable: "",
    fiscalLines: [{ id: "l1", base: 100, vatRate: 21, vatAmount: 21, total: 121 }],
    formData: {
      nif_entidad: "", nombre_entidad: "",
      nif_receptor: "", nombre_receptor: "",
      numero_factura: "F1", fecha_expedicion: "2026-05-08",
      total_euros: "121", concepto: "x",
      ...over,
    },
  };
}

describe("generateIntermegaCSVsByCliente", () => {
  const FECHA = new Date("2026-05-08T12:00:00Z");

  it("produces one file per (NIF cliente × tipo); ordered alpha, emitidas before recibidas", () => {
    const invoices = new Map<string, ApprovedInvoiceData>([
      // Cliente B222: ingresos (entidad = our client)
      ["d1", inv("ingresos", { nif_entidad: "B222", nombre_entidad: "BETA SL", nif_receptor: "X1", nombre_receptor: "Cust1" })],
      // Cliente B111: gastos (receptor = our client)
      ["d2", inv("gastos",   { nif_receptor: "B111", nombre_receptor: "ACME SL", nif_entidad: "P1", nombre_entidad: "Prov1" })],
      // Cliente B111: ingresos
      ["d3", inv("ingresos", { nif_entidad: "B111", nombre_entidad: "ACME SL", nif_receptor: "X2", nombre_receptor: "Cust2" })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, FECHA);
    expect(files.map(f => f.filename)).toEqual([
      "20260508_B111_01_emitidas.csv",
      "20260508_B111_02_recibidas.csv",
      "20260508_B222_03_emitidas.csv",
    ]);
    expect(files[0].nifCliente).toBe("B111");
    expect(files[0].nombreCliente).toBe("ACME SL");
    expect(files[0].tipo).toBe("emitidas");
    expect(files[0].rowCount).toBe(1);
  });

  it("groups invoices with empty derived NIF under SIN_CLIENTE", () => {
    const invoices = new Map([
      ["d1", inv("ingresos", { /* no nif_entidad */ nif_receptor: "Cust" })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, FECHA);
    expect(files).toHaveLength(1);
    expect(files[0].filename).toBe("20260508_SIN_CLIENTE_01_emitidas.csv");
    expect(files[0].nifCliente).toBe("SIN_CLIENTE");
  });

  it("CSV content has BOM, CRLF, header, and one data row per fiscal line", () => {
    const invoices = new Map([
      ["d1", inv("gastos", { nif_receptor: "B111", nombre_receptor: "ACME", nif_entidad: "P1", nombre_entidad: "PROV" })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, FECHA);
    const csv = files[0].content;
    expect(csv.charCodeAt(0)).toBe(0xFEFF);                          // BOM
    expect(csv).toMatch(/FECHA;SERIE;Nº FACTURA;NOMBRE CLI-PRO/);    // header
    expect(csv).toContain("\r\n");                                   // CRLF
    expect(csv).toContain("PROV");                                   // CLI-PRO = entidad on gastos
    expect(csv).toContain("P1");
  });

  it("derives NIF from live formData, ignoring stale formData.nif_cliente", () => {
    // Operator edited nif_receptor; the stale nif_cliente snapshot must NOT be used.
    const invoices = new Map([
      ["d1", inv("gastos", {
        nif_cliente: "STALE_OLD",                  // must be ignored
        nombre_cliente: "STALE NAME",
        nif_receptor: "B111", nombre_receptor: "ACME",
        nif_entidad: "P1", nombre_entidad: "PROV",
      })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, FECHA);
    expect(files[0].nifCliente).toBe("B111");
    expect(files[0].nombreCliente).toBe("ACME");
  });

  it("sanitizes NIF for filename: uppercases, strips non-alphanumeric", () => {
    const invoices = new Map([
      ["d1", inv("gastos", { nif_receptor: " b-111/22 ", nombre_receptor: "X" })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, FECHA);
    expect(files[0].filename).toBe("20260508_B11122_01_recibidas.csv");
  });

  it("empty invoice map → empty array", () => {
    expect(generateIntermegaCSVsByCliente(new Map(), FECHA)).toEqual([]);
  });
});

describe("intermegaRowsFor — no fabrica filas cuando faltan líneas fiscales (Error 1: no inventar IVA cero)", () => {
  // Decisión: una factura sin desglose fiscal NO debe emitir una fila con
  // BASE=total y CUOTA 0,00 — eso importaría en Intermega una venta sin IVA
  // repercutido (dato fiscal incorrecto). En su lugar no se emite fila y la UI
  // avisa al operario (ver export-stage). "nunca inventar datos".
  it("fiscalLines vacío → 0 filas (no se inventa una fila con IVA cero)", () => {
    const rows = intermegaRowsFor(
      makeInvoice({
        libro: "ingresos",
        fiscalLines: [],
        formData: { total_euros: "121", numero_factura: "F-9", fecha_expedicion: "2026-05-08" },
      })
    );
    expect(rows).toHaveLength(0);
  });

  it("generateIntermegaCSVsByCliente: una emitida sin líneas fiscales NO añade fila de datos (solo cabecera)", () => {
    const invoices = new Map<string, ApprovedInvoiceData>([
      ["d1", makeInvoice({ libro: "ingresos", fiscalLines: [], formData: { total_euros: "121" } })],
    ]);
    const files = generateIntermegaCSVsByCliente(invoices, new Date("2026-05-08T12:00:00Z"));
    const emitidas = files.find((f) => f.tipo === "emitidas");
    expect(emitidas).toBeDefined();
    const lines = emitidas!.content.split("\r\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1); // solo cabecera, sin fila fabricada
  });
});
