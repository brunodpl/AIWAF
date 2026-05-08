import { describe, it, expect } from "vitest";
import { intermegaRowsFor } from "../csv";
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
