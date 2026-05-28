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
    await screen.findByText("Recibidas (compras/gastos)");

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
    await screen.findByText("Recibidas (compras/gastos)");

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

describe("BooksManager — recuperación tras timeout (F4)", () => {
  // Tras la reversión del auto-jump (2026-05-28), el recovery del catch
  // acumula las entries reconstruidas vía `recoverSplitEntriesFromBooks` en
  // `accumulatedUploads` y se queda en Gestión — mismo contrato que el happy
  // path. El operario decide cuándo escanear con "Escanear N Factura(s)".
  it("recupera el split tras timeout sin transicionar a Pre-revisión", async () => {
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    api.uploadFiles.mockRejectedValue(
      new Error("Request timed out. The server may still be processing."),
    );
    const onUploadComplete = vi.fn();

    render(
      <BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />,
    );

    await screen.findByText("Recibidas (compras/gastos)");
    const initialFetchBooksCalls = api.fetchBooks.mock.calls.length;

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    const fakeFile = new File(["x"], "nueva_factura.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", { value: [fakeFile], configurable: true });
    fireEvent.change(gastoInput);

    // El catch hace fetchBooks de nuevo para reconstruir las entries vía
    // recoverSplitEntriesFromBooks. Esperamos a que ese fetch ocurra.
    await waitFor(
      () => expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(initialFetchBooksCalls),
      { timeout: 3000 },
    );
    // Y verificamos que NO se transiciona a Pre-revisión.
    await new Promise((r) => setTimeout(r, 100));
    expect(onUploadComplete).not.toHaveBeenCalled();
  });
});

describe("BooksManager — happy path: trigger automático de Pre-revisión", () => {
  beforeEach(() => {
    api.fetchBooks.mockReset();
    api.uploadFiles.mockReset();
    api.runPipeline.mockReset();
    api.deleteBookFile.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("T1: tras upload con detected_summary.total_invoices > 0, NO llama onUploadComplete (auto-jump revertido)", async () => {
    // Backend ya tiene los hijos del split sincronizados (caso ideal happy path).
    api.fetchBooks.mockResolvedValue({
      books: makeBooksWithSplitChildren(),
    });

    // Respuesta del POST /upload con 3 facturas detectadas en 1 archivo.
    api.uploadFiles.mockResolvedValue({
      uploaded: ["nueva_factura.pdf"],
      errors: [],
      already_processed: [],
      files: [
        {
          name: "nueva_factura.pdf",
          size_kb: 30,
          added: "2026-01-01T00:00:00Z",
          status: "split",
          pre_scan: {
            status: "split",
            children: [
              { doc_id: "nueva_factura__1of3" },
              { doc_id: "nueva_factura__2of3" },
              { doc_id: "nueva_factura__3of3" },
            ],
          },
        },
      ],
      detected_summary: { total_files: 1, total_invoices: 3 },
    });

    const onUploadComplete = vi.fn();

    render(
      <BooksManager
        onPipelineStart={() => {}}
        onUploadComplete={onUploadComplete}
      />
    );

    // Esperar al render inicial (patrón usado por los tests existentes).
    await screen.findByText("Recibidas (compras/gastos)");

    // Localizar el input file del libro "gastos" (primer book card renderizado).
    const fileInputs = document.querySelectorAll('input[type="file"]');
    const gastoInput = fileInputs[0] as HTMLInputElement;
    expect(gastoInput).toBeTruthy();

    const fakeFile = new File(["dummy content"], "nueva_factura.pdf", {
      type: "application/pdf",
    });
    Object.defineProperty(gastoInput, "files", {
      value: [fakeFile],
      configurable: true,
    });
    fireEvent.change(gastoInput);

    // Tras la reversión del auto-jump (2026-05-28), el upload acumula en
    // `accumulatedUploads` y se queda en Gestión — NO transiciona a
    // Pre-revisión. Esperamos a que el handler termine el bloque finally
    // (que siempre llama loadBooks → fetchBooks) y verificamos que el
    // callback no se ha disparado.
    const initialFetchBooksCalls = api.fetchBooks.mock.calls.length;
    await waitFor(
      () => expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(initialFetchBooksCalls),
      { timeout: 3000 }
    );
    await new Promise((r) => setTimeout(r, 100));
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it("T2: con detected_summary.total_invoices = 0 (pre_scan_failed) NO llama onUploadComplete", async () => {
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    api.uploadFiles.mockResolvedValue({
      uploaded: ["mala.pdf"],
      errors: [],
      already_processed: [],
      files: [
        {
          name: "mala.pdf",
          size_kb: 10,
          added: "2026-01-01T00:00:00Z",
          status: "pre_scan_failed",
          pre_scan: { status: "failed", error: "vision_timeout" },
        },
      ],
      detected_summary: { total_files: 1, total_invoices: 0 },
    });

    const onUploadComplete = vi.fn();
    render(<BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />);
    await screen.findByText("Recibidas (compras/gastos)");
    const initialFetchBooksCalls = api.fetchBooks.mock.calls.length;

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    const file = new File(["x"], "mala.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", { value: [file], configurable: true });
    fireEvent.change(gastoInput);

    // Esperar a que el handler termine de ejecutar el bloque finally
    // (que siempre llama loadBooks → fetchBooks). Mas determinista que un sleep.
    await waitFor(() =>
      expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(initialFetchBooksCalls)
    );
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it("T3: re-upload duplicado (uploaded vacío, already_processed > 0) NO llama onUploadComplete", async () => {
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    api.uploadFiles.mockResolvedValue({
      uploaded: [],
      errors: [],
      already_processed: [
        {
          file: "duplicada.pdf",
          original: "compras_duplicada",
          original_status: "done",
          pending: [],
        },
      ],
      files: [],
      detected_summary: { total_files: 0, total_invoices: 0 },
    });

    const onUploadComplete = vi.fn();
    render(<BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />);
    await screen.findByText("Recibidas (compras/gastos)");
    const initialFetchBooksCalls = api.fetchBooks.mock.calls.length;

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    const file = new File(["x"], "duplicada.pdf", { type: "application/pdf" });
    Object.defineProperty(gastoInput, "files", { value: [file], configurable: true });
    fireEvent.change(gastoInput);

    // Esperar a que el handler termine de ejecutar el bloque finally
    // (que siempre llama loadBooks → fetchBooks). Mas determinista que un sleep.
    await waitFor(() =>
      expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(initialFetchBooksCalls)
    );
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it("T4: dos uploads consecutivos NO llaman al callback (auto-jump revertido); el acumulador sigue funcionando", async () => {
    // Mock /api/books con los hijos que ambos uploads van a esperar (primer__*
    // y segundo__*), para que pollBooksUntilSynced retorne en la primera vuelta
    // y el test no espere los 20s del deadline interno.
    const booksWithAllChildren = (): Book[] => [
      {
        id: "gastos",
        label: "Libro de Gastos y Compras",
        folder: "compras",
        libro_short: "compras",
        files: [
          { name: "primer__1of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
          { name: "primer__2of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
          { name: "primer__3of3.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
          { name: "segundo__1of2.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
          { name: "segundo__2of2.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "uploaded" },
        ],
      },
      { id: "ingresos", label: "Libro de Ingresos y Ventas", folder: "ventas", files: [] },
      { id: "bienes", label: "Libro de Bienes de Inversión", folder: "bienes", files: [] },
    ];
    api.fetchBooks.mockResolvedValue({ books: booksWithAllChildren() });

    // Primer upload: 3 facturas.
    api.uploadFiles.mockResolvedValueOnce({
      uploaded: ["primer.pdf"],
      errors: [],
      already_processed: [],
      files: [
        {
          name: "primer.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "split",
          pre_scan: { status: "split", children: [
            { doc_id: "primer__1of3" },
            { doc_id: "primer__2of3" },
            { doc_id: "primer__3of3" },
          ]},
        },
      ],
      detected_summary: { total_files: 1, total_invoices: 3 },
    });
    // Segundo upload: 2 facturas.
    api.uploadFiles.mockResolvedValueOnce({
      uploaded: ["segundo.pdf"],
      errors: [],
      already_processed: [],
      files: [
        {
          name: "segundo.pdf", size_kb: 10, added: "2026-01-01T00:00:00Z", status: "split",
          pre_scan: { status: "split", children: [
            { doc_id: "segundo__1of2" },
            { doc_id: "segundo__2of2" },
          ]},
        },
      ],
      detected_summary: { total_files: 1, total_invoices: 2 },
    });

    const onUploadComplete = vi.fn();
    render(<BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />);
    await screen.findByText("Recibidas (compras/gastos)");

    // Drop 1 — esperamos a que el handler termine el finally (loadBooks).
    let fetchBaseline = api.fetchBooks.mock.calls.length;
    let gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "primer.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);
    await waitFor(
      () => expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(fetchBaseline),
      { timeout: 3000 },
    );

    // Drop 2 — re-query del input (el componente puede haber re-renderizado).
    fetchBaseline = api.fetchBooks.mock.calls.length;
    gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "segundo.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);
    await waitFor(
      () => expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(fetchBaseline),
      { timeout: 3000 },
    );

    // Tras la reversión del auto-jump (2026-05-28), ninguno de los dos uploads
    // dispara onUploadComplete: ambos acumulan internamente y se quedan en
    // Gestión. La transición la dispara el botón "Escanear N Factura(s)".
    await new Promise((r) => setTimeout(r, 100));
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it("T5: timeout recovery del catch NO llama al callback (auto-jump revertido); acumula entries reconstruidas", async () => {
    // Simulamos un timeout/abort en uploadFiles, pero el backend dejó los hijos
    // visibles en /api/books (caso F4 cubierto por el catch del handler).
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    const abortErr = new Error("timed out — proxy 504");
    api.uploadFiles.mockRejectedValueOnce(abortErr);

    const onUploadComplete = vi.fn();
    render(<BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />);
    await screen.findByText("Recibidas (compras/gastos)");
    const initialFetchBooksCalls = api.fetchBooks.mock.calls.length;

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "nueva_factura.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled());
    // El catch hace fetchBooks de nuevo para reconstruir entries vía
    // recoverSplitEntriesFromBooks. Esperamos a que ese fetch ocurra.
    await waitFor(
      () => expect(api.fetchBooks.mock.calls.length).toBeGreaterThan(initialFetchBooksCalls),
      { timeout: 3000 },
    );
    // Tras la reversión del auto-jump (2026-05-28), el recovery acumula
    // entries en `accumulatedUploads` pero NO transiciona — consistencia con
    // el happy path.
    await new Promise((r) => setTimeout(r, 100));
    expect(onUploadComplete).not.toHaveBeenCalled();
  });
});
