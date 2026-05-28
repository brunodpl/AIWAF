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

    await screen.findByText("Recibidas (compras/gastos)");

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

  it("T1: tras upload con detected_summary.total_invoices > 0, llama onUploadComplete con los archivos del bookId", async () => {
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

    // Esperar a que el upload y el acumulador completen, y que el callback
    // se dispare 1 vez.
    await waitFor(
      () => expect(onUploadComplete).toHaveBeenCalledTimes(1),
      { timeout: 3000 }
    );

    const [bookId, files] = onUploadComplete.mock.calls[0];
    expect(bookId).toBe("gastos");
    expect(files).toHaveLength(1);
    expect(files[0].name).toBe("nueva_factura.pdf");
    expect(files[0].pre_scan?.status).toBe("split");
    expect(files[0].pre_scan?.children).toHaveLength(3);
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

  it("T4: dos uploads consecutivos al mismo libro llaman al callback 2 veces; la 2ª solo trae los nuevos", async () => {
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

    // Drop 1
    let gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "primer.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);
    await waitFor(() => expect(onUploadComplete).toHaveBeenCalledTimes(1), { timeout: 3000 });

    // Drop 2 — re-query del input (el componente puede haber re-renderizado).
    gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "segundo.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);
    await waitFor(() => expect(onUploadComplete).toHaveBeenCalledTimes(2), { timeout: 3000 });

    // Primer call: solo el primer.pdf (no contaminacion del estado vacio inicial).
    const [bookIdFirst, filesFirst] = onUploadComplete.mock.calls[0];
    expect(bookIdFirst).toBe("gastos");
    expect(filesFirst.map((f: { name: string }) => f.name)).toEqual(["primer.pdf"]);

    // Segundo call: solo el segundo.pdf (no se acumula con el primero — el
    // callback recibe el diff por upload, no el cumulativo).
    const [bookIdA, filesA] = onUploadComplete.mock.calls[1];
    expect(bookIdA).toBe("gastos");
    expect(filesA.map((f: { name: string }) => f.name)).toEqual(["segundo.pdf"]);
  });

  it("T5: timeout recovery del catch llama al callback 1 vez (NO doble: happy y catch son mutuamente excluyentes)", async () => {
    // Simulamos un timeout/abort en uploadFiles, pero el backend dejó los hijos
    // visibles en /api/books (caso F4 cubierto por el catch del handler).
    api.fetchBooks.mockResolvedValue({ books: makeBooksWithSplitChildren() });
    const abortErr = new Error("timed out — proxy 504");
    api.uploadFiles.mockRejectedValueOnce(abortErr);

    const onUploadComplete = vi.fn();
    render(<BooksManager onPipelineStart={() => {}} onUploadComplete={onUploadComplete} />);
    await screen.findByText("Recibidas (compras/gastos)");

    const gastoInput = document.querySelectorAll('input[type="file"]')[0] as HTMLInputElement;
    Object.defineProperty(gastoInput, "files", {
      value: [new File(["x"], "nueva_factura.pdf", { type: "application/pdf" })],
      configurable: true,
    });
    fireEvent.change(gastoInput);

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled());
    // El catch hace fetchBooks de nuevo para recuperar entries vía
    // recoverSplitEntriesFromBooks. Verificamos que el callback dispara 1 sola vez.
    await waitFor(() => expect(onUploadComplete).toHaveBeenCalledTimes(1), { timeout: 3000 });
    // Y nada más después (el happy path no entra al catch).
    await new Promise((r) => setTimeout(r, 100));
    expect(onUploadComplete).toHaveBeenCalledTimes(1);
  });
});
