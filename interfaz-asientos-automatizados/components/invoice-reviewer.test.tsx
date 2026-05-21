import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, within } from "@testing-library/react";

// jsdom no implementa ResizeObserver/matchMedia, de los que dependen ScrollArea
// (Radix) y ResizablePanelGroup (react-resizable-panels) al montar el reviewer.
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
// Radix puede llamar a estos en interacciones con Select; stub defensivo.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(Element.prototype as any).scrollIntoView = (Element.prototype as any).scrollIntoView ?? (() => {});
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(Element.prototype as any).hasPointerCapture = (Element.prototype as any).hasPointerCapture ?? (() => false);

// Mocks del módulo de API. ``vi.hoisted`` permite referenciarlos dentro del
// factory de ``vi.mock`` (que vitest eleva al tope del fichero).
const api = vi.hoisted(() => ({
  fetchInvoices: vi.fn(),
  fetchInvoiceDetail: vi.fn(),
  fetchPipelineBatch: vi.fn(),
  sendInvoiceAction: vi.fn(),
  deleteBookFile: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  API_URL: "",
  fetchInvoices: api.fetchInvoices,
  fetchInvoiceDetail: api.fetchInvoiceDetail,
  fetchPipelineBatch: api.fetchPipelineBatch,
  sendInvoiceAction: api.sendInvoiceAction,
  deleteBookFile: api.deleteBookFile,
  // En el test, fetchInvoiceDetail ya devuelve un InvoiceDocument listo, así
  // que la transformación es la identidad.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  transformToInvoice: (detail: any) => detail,
}));

import { InvoiceReviewer } from "@/components/invoice-reviewer";

// Tres facturas en el array CRUDO del backend, en este orden: A, B, C.
const RAW_IDS = ["doc-A", "doc-B", "doc-C"] as const;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeSummary(id: string): any {
  return {
    id,
    decision_global: "auto",
    timestamp: "2026-01-01T00:00:00Z",
    nif_entidad: "B00000000",
    nombre_entidad: `Entidad ${id}`,
    numero_factura: `F-${id}`,
    total_euros: 100,
    rejection_count: 0,
    libro: "compras",
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function makeDoc(id: string): any {
  return {
    id,
    status: "pending",
    imageUrl: "",
    fileType: "image",
    libro: "gastos",
    invoice_filename: `${id}.pdf`,
    rejection_count: 0,
    fields: [
      { id: "numero_factura", label: "Nº Factura", value: `F-${id}`, status: "auto", confidence: 100, group: 2 },
    ],
    fiscalLines: [
      { id: "l1", base: 100, vatRate: 21, vatAmount: 21, total: 121 },
    ],
  };
}

function baseProps() {
  return {
    approvedInvoices: new Map(),
    rejectedInvoices: new Set<string>(),
    onApprove: vi.fn(),
    onReject: vi.fn(),
    onExport: vi.fn(),
    onClearFocus: vi.fn(),
  };
}

beforeEach(() => {
  api.fetchInvoices.mockResolvedValue({ invoices: RAW_IDS.map(makeSummary) });
  api.fetchInvoiceDetail.mockImplementation(async (id: string) => makeDoc(id));
  api.fetchPipelineBatch.mockResolvedValue({ in_flight: false, books: [] });
  api.sendInvoiceAction.mockResolvedValue(undefined);
  api.deleteBookFile.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function clickApproveAndGetTargetId(): Promise<string> {
  // Espera a que cargue la lista + el detalle, dejando el botón Aprobar activo.
  const approveBtn = await screen.findByRole("button", { name: /^aprobar$/i });
  await waitFor(() => expect((approveBtn as HTMLButtonElement).disabled).toBe(false));

  fireEvent.click(approveBtn);

  await waitFor(() => expect(api.sendInvoiceAction).toHaveBeenCalledTimes(1));
  return api.sendInvoiceAction.mock.calls[0][0] as string;
}

describe("InvoiceReviewer index-divergence (filtered visible vs raw)", () => {
  it("focus mode: aprueba la factura MOSTRADA (visible[idx]), no la del índice crudo", async () => {
    // focusDocIds filtra la lista visible a SOLO doc-B. La factura mostrada en
    // currentIdx=0 es doc-B, pero raw[0] es doc-A. La acción debe accionar la
    // mostrada (doc-B), no doc-A.
    render(<InvoiceReviewer {...baseProps()} focusDocIds={["doc-B"]} />);

    const targetId = await clickApproveAndGetTargetId();

    expect(targetId).toBe("doc-B");
    expect(targetId).not.toBe("doc-A");
  });

  it("batch mode: aprueba la factura MOSTRADA cuando el lote filtra un subconjunto", async () => {
    // El lote (/api/pipeline/batch) reporta solo doc-B y doc-C. La lista visible
    // queda [doc-B, doc-C]; en currentIdx=0 se muestra doc-B mientras raw[0]=doc-A.
    api.fetchPipelineBatch.mockResolvedValue({
      in_flight: true,
      books: [{ files: [{ doc_id: "doc-B" }, { doc_id: "doc-C" }] }],
    });

    render(<InvoiceReviewer {...baseProps()} />);

    const targetId = await clickApproveAndGetTargetId();

    expect(targetId).toBe("doc-B");
    expect(targetId).not.toBe("doc-A");
  });

  it("hard delete (focus): borra el FICHERO de la factura mostrada, no la del índice crudo", async () => {
    // doc-B ya fue rechazada una vez (rejection_count=1): el botón pasa a
    // "Eliminar definitivamente" y abre el diálogo de borrado. El borrado debe
    // apuntar al fichero de doc-B (mostrada), no al de raw[0]=doc-A.
    const summaries = RAW_IDS.map((id) => ({
      ...makeSummary(id),
      rejection_count: id === "doc-B" ? 1 : 0,
    }));
    api.fetchInvoices.mockResolvedValue({ invoices: summaries });

    render(<InvoiceReviewer {...baseProps()} focusDocIds={["doc-B"]} />);

    const hardDeleteBtn = await screen.findByRole("button", { name: /eliminar definitivamente/i });
    await waitFor(() => expect((hardDeleteBtn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(hardDeleteBtn);

    // Confirmar dentro del AlertDialog (mismo texto que el botón del footer).
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /eliminar definitivamente/i }));

    await waitFor(() => expect(api.deleteBookFile).toHaveBeenCalledTimes(1));
    // deleteBookFile(bookId, filename) → filename del documento mostrado (doc-B).
    expect(api.deleteBookFile.mock.calls[0][1]).toBe("doc-B.pdf");
    expect(api.deleteBookFile.mock.calls[0][1]).not.toBe("doc-A.pdf");
  });
});
