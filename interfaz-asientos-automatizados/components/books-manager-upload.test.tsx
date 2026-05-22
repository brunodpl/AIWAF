import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";

// jsdom no implementa ResizeObserver/matchMedia, de los que depende ScrollArea
// (Radix) al montar el BooksManager.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).ResizeObserver =
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver ?? ResizeObserverStub;
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

const api = vi.hoisted(() => ({
  fetchBooks: vi.fn(),
  uploadFiles: vi.fn(),
  runPipeline: vi.fn(),
  deleteBookFile: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  API_URL: "",
  fetchBooks: api.fetchBooks,
  uploadFiles: api.uploadFiles,
  runPipeline: api.runPipeline,
  deleteBookFile: api.deleteBookFile,
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    message: vi.fn(),
  },
}));

import { BooksManager, recoverSplitEntriesFromBooks } from "@/components/books-manager";
import type { Book } from "@/lib/types";

function makeBooksWithSplitChildren(): Book[] {
  return [
    {
      id: "gastos",
      label: "Libro de Gastos y Compras",
      folder: "compras",
      libro_short: "compras",
      files: [
        { name: "nueva_factura__1of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
        { name: "nueva_factura__2of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
        { name: "nueva_factura__3of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
      ],
    },
    { id: "ingresos", label: "Libro de Ingresos y Ventas", folder: "ventas", files: [] },
    { id: "bienes", label: "Libro de Bienes de Inversión", folder: "bienes", files: [] },
  ];
}

function makeBooks(): Book[] {
  return [
    {
      id: "gastos",
      label: "Libro de Gastos y Compras",
      folder: "compras",
      libro_short: "compras",
      files: [
        {
          name: "factura_existente.pdf",
          size_kb: 42.0,
          added: "2026-01-01T00:00:00Z",
          status: "uploaded",
        },
      ],
    },
    { id: "ingresos", label: "Libro de Ingresos y Ventas", folder: "ventas", files: [] },
    { id: "bienes", label: "Libro de Bienes de Inversión", folder: "bienes", files: [] },
  ];
}

beforeEach(() => {
  // Primera llamada: carga inicial. Llamadas posteriores (reconciliación): devuelve
  // los mismos datos para verificar que se re-fetcha la lista desde el servidor.
  api.fetchBooks.mockResolvedValue({ books: makeBooks() });
  api.uploadFiles.mockRejectedValue(new Error("Network error: connection refused"));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BooksManager — resiliencia a upload fallido (500/timeout)", () => {
  it("reconcilia la lista desde /api/books incluso cuando la subida falla", async () => {
    render(
      <BooksManager
        onPipelineStart={() => {}}
        onUploadComplete={() => {}}
      />
    );

    // Esperar la carga inicial de libros
    await screen.findByText("Libro de Gastos y Compras");

    // Contar cuántas veces fetchBooks fue llamado hasta ahora (carga inicial)
    const callsAfterMount = api.fetchBooks.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThanOrEqual(1);

    // Localizar el input de archivo oculto del libro "gastos"
    // El componente renderiza un input[type=file] dentro de cada book card.
    // Hay dos inputs por libro cuando hay archivos; buscamos el primero del DOM.
    const fileInputs = document.querySelectorAll('input[type="file"]');
    // El primer input corresponde al libro "gastos" (primer book card renderizado)
    const gastoInput = fileInputs[0] as HTMLInputElement;
    expect(gastoInput).toBeTruthy();

    // Disparar la selección de un archivo para triggear handleFileSelect → handleDrop
    const fakeFile = new File(["dummy content"], "nueva_factura.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", {
      value: [fakeFile],
      configurable: true,
    });
    fireEvent.change(gastoInput);

    // Esperar a que el upload falle y la reconciliación ocurra
    await waitFor(
      () => {
        // fetchBooks debe haber sido llamado MÁS veces que tras el montaje inicial,
        // lo que confirma que loadBooks() fue invocado después del error de subida.
        expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(callsAfterMount);
      },
      { timeout: 3000 }
    );
  });

  it("muestra un toast no destructivo (warning/message) cuando la subida falla, sin limpiar la lista", async () => {
    const { toast } = await import("sonner");

    render(
      <BooksManager
        onPipelineStart={() => {}}
        onUploadComplete={() => {}}
      />
    );

    // Esperar carga inicial
    await screen.findByText("Libro de Gastos y Compras");

    const fileInputs = document.querySelectorAll('input[type="file"]');
    const gastoInput = fileInputs[0] as HTMLInputElement;

    const fakeFile = new File(["dummy content"], "nueva_factura.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", {
      value: [fakeFile],
      configurable: true,
    });
    fireEvent.change(gastoInput);

    // Esperar a que el upload falle y el toast se muestre
    await waitFor(
      () => {
        // Debe mostrar warning o message — NO debe limpiar la lista de archivos
        // (el archivo existente "factura_existente.pdf" sigue visible tras el error)
        expect(screen.queryByText("factura_existente.pdf")).toBeTruthy();
      },
      { timeout: 3000 }
    );

    // El toast de warning/message debe haber sido invocado (no solo error)
    // Al menos uno de los mecanismos no destructivos debe haber sido llamado
    const warningCalled = (toast.warning as ReturnType<typeof vi.fn>).mock.calls.length > 0;
    const messageCalled =
      "message" in toast &&
      (toast.message as ReturnType<typeof vi.fn>).mock.calls.length > 0;
    expect(warningCalled || messageCalled).toBe(true);
  });
});

describe("recoverSplitEntriesFromBooks (F4)", () => {
  it("reconstruye un padre virtual split desde los hijos en /api/books", () => {
    const book = makeBooksWithSplitChildren()[0];
    const entries = recoverSplitEntriesFromBooks(book, ["nueva_factura.pdf"]);
    expect(entries).toHaveLength(1);
    expect(entries[0].name).toBe("nueva_factura.pdf");
    expect(entries[0].pre_scan?.status).toBe("split");
    expect(entries[0].pre_scan?.detected_invoices).toBe(3);
    expect(entries[0].pre_scan?.children).toHaveLength(3);
  });

  it("devuelve vacío si no hay hijos (no hubo split ni el backend terminó)", () => {
    const book: Book = {
      id: "gastos",
      label: "x",
      folder: "compras",
      files: [{ name: "otro.pdf", size_kb: 1, added: "2026-01-01T00:00:00Z", status: "uploaded" }],
    };
    expect(recoverSplitEntriesFromBooks(book, ["nueva_factura.pdf"])).toHaveLength(0);
  });
});

describe("BooksManager — recuperación de Pre-revisión tras timeout (F4)", () => {
  it("transiciona a Pre-revisión si el split se completó pese al timeout", async () => {
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    api.uploadFiles.mockRejectedValue(
      new Error("Request timed out. The server may still be processing."),
    );
    const onUploadComplete = vi.fn();

    render(
      <BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />,
    );

    await screen.findByText("Libro de Gastos y Compras");

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    const fakeFile = new File(["x"], "nueva_factura.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", { value: [fakeFile], configurable: true });
    fireEvent.change(gastoInput);

    await waitFor(() => expect(onUploadComplete).toHaveBeenCalled(), { timeout: 3000 });

    const [bookId, entries] = onUploadComplete.mock.calls[0];
    expect(bookId).toBe("gastos");
    expect(entries[0].pre_scan.status).toBe("split");
    expect(entries[0].pre_scan.detected_invoices).toBe(3);
  });
});
