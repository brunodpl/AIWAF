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

import { BooksManager } from "@/components/books-manager";
import type { Book } from "@/lib/types";

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
