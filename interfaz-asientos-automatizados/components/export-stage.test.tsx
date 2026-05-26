import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";

// jsdom no implementa ResizeObserver/matchMedia, de los que depende Radix
// (AlertDialog) al montar.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).ResizeObserver = (globalThis as any).ResizeObserver ?? ResizeObserverStub;
if (!globalThis.matchMedia) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  });
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(Element.prototype as any).scrollIntoView = (Element.prototype as any).scrollIntoView ?? (() => {});

// URL.createObjectURL no existe en jsdom; la descarga real lo usa.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(URL as any).createObjectURL = (URL as any).createObjectURL ?? vi.fn(() => "blob:test");
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(URL as any).revokeObjectURL = (URL as any).revokeObjectURL ?? vi.fn();

const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: toastMock }));

import { ExportStage } from "@/components/export-stage";
import type { ApprovedInvoiceData } from "@/lib/types";

function makeInvoice(formOver: Record<string, string>): ApprovedInvoiceData {
  return {
    libro: "ingresos",
    cuenta_contable: "700000",
    fiscalLines: [{ id: "l1", base: 100, vatRate: 21, vatAmount: 21, total: 121 }],
    formData: {
      nif_entidad: "B111",
      nombre_entidad: "ACME SL",
      nif_receptor: "X1",
      nombre_receptor: "Cust",
      numero_factura: "F-1",
      fecha_expedicion: "2026-05-08",
      total_euros: "121",
      concepto: "Servicios",
      cuenta_contable: "700000",
      ...formOver,
    },
  };
}

function clickPerFileDownload() {
  // El botón por-fichero tiene nombre accesible exacto "Descargar"
  // (el del footer es "Descargar ZIP (N)").
  fireEvent.click(screen.getByRole("button", { name: "Descargar" }));
}

describe("ExportStage — aviso de FECHA / Nº FACTURA ausentes antes de descargar", () => {
  afterEach(() => {
    cleanup();
    toastMock.success.mockClear();
    toastMock.error.mockClear();
    toastMock.warning.mockClear();
  });

  it("avisa con dialog si una factura no tiene fecha_expedicion (cuenta presente)", () => {
    const invoices = new Map<string, ApprovedInvoiceData>([
      ["d1", makeInvoice({ fecha_expedicion: "" })],
    ]);
    render(<ExportStage approvedInvoices={invoices} onBack={() => {}} />);
    clickPerFileDownload();
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText(/Nº FACTURA/i)).toBeTruthy();
    // No debe descargar mientras el aviso está abierto.
    expect(toastMock.success).not.toHaveBeenCalled();
  });

  it("avisa con dialog si una factura no tiene numero_factura (cuenta presente)", () => {
    const invoices = new Map<string, ApprovedInvoiceData>([
      ["d1", makeInvoice({ numero_factura: "" })],
    ]);
    render(<ExportStage approvedInvoices={invoices} onBack={() => {}} />);
    clickPerFileDownload();
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText(/FECHA/i)).toBeTruthy();
    expect(toastMock.success).not.toHaveBeenCalled();
  });

  it("descarga sin aviso cuando FECHA, Nº FACTURA y cuenta están presentes", () => {
    const invoices = new Map<string, ApprovedInvoiceData>([
      ["d1", makeInvoice({})],
    ]);
    render(<ExportStage approvedInvoices={invoices} onBack={() => {}} />);
    clickPerFileDownload();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(toastMock.success).toHaveBeenCalled();
  });
});
