import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { useState } from "react";

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
  },
}));

import { BooksManager, buildPendingPreReviewEntries } from "@/components/books-manager";
import type { Book, BookFile } from "@/lib/types";

const PARENT_NAME = "1_2_3_merged.pdf";
const CHILD_NAMES = [
  "1_2_3_merged__1of3.pdf",
  "1_2_3_merged__2of3.pdf",
  "1_2_3_merged__3of3.pdf",
];

// El padre virtual tal como lo conserva el padre (page.tsx) tras un round-trip
// por Pre-revisión: ``pre_scan.status === "split"`` con N hijos.
function makeParentFile(): BookFile {
  return {
    name: PARENT_NAME,
    size_kb: 170.9,
    added: "2026-01-01T00:00:00Z",
    pre_scan: {
      status: "split",
      n_pages: 3,
      detected_invoices: 3,
      children: CHILD_NAMES.map((_, i) => ({
        doc_id: `1_2_3_merged__${i + 1}of3`,
        folder_name: `compras_1_2_3_merged__${i + 1}of3`,
        status: "uploaded",
      })),
      error: null,
    },
  };
}

// Los hijos viven en el inbox (/api/books). El padre original fue movido a
// ``_originales/`` por el splitter y NO aparece aquí.
function makeBooks(): Book[] {
  return [
    {
      id: "gastos",
      label: "Libro de Gastos y Compras",
      folder: "compras",
      libro_short: "compras",
      files: CHILD_NAMES.map((name) => ({
        name,
        size_kb: 56.9,
        added: "2026-01-01T00:00:00Z",
        status: "uploaded",
      })),
    },
    { id: "ingresos", label: "Libro de Ingresos y Ventas", folder: "ventas", files: [] },
    { id: "bienes", label: "Libro de Bienes de Inversión", folder: "bienes", files: [] },
  ];
}

// Wrapper con estado: replica el contrato de page.tsx — al borrar un archivo
// en BooksManager el padre retira la entrada de ``preReviewEntries``.
function Harness() {
  const [byBook, setByBook] = useState<Record<string, BookFile[]>>({
    gastos: [makeParentFile()],
  });
  return (
    <BooksManager
      onPipelineStart={() => {}}
      onUploadComplete={() => {}}
      parentPreReviewFilesByBook={byBook}
      onFileRemoved={(bookId, fileName) =>
        setByBook((prev) => {
          const files = (prev[bookId] || []).filter((f) => f.name !== fileName);
          const next = { ...prev };
          if (files.length) next[bookId] = files;
          else delete next[bookId];
          return next;
        })
      }
    />
  );
}

beforeEach(() => {
  api.fetchBooks.mockResolvedValue({ books: makeBooks() });
  api.deleteBookFile.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BooksManager — borrado de PDF multi-factura (padre de split)", () => {
  it("muestra UNA fila (padre virtual con badge) y oculta los hijos", async () => {
    render(<Harness />);
    expect(await screen.findByText(PARENT_NAME)).toBeTruthy();
    expect(screen.getByText(/3 facturas/i)).toBeTruthy();
    for (const child of CHILD_NAMES) {
      expect(screen.queryByText(child)).toBeNull();
    }
  });

  it("al pulsar la X borra el grupo: DELETE con el nombre del padre, y ni el padre ni los hijos quedan visibles", async () => {
    render(<Harness />);
    const x = await screen.findByLabelText(`Eliminar ${PARENT_NAME}`);
    fireEvent.click(x);

    await waitFor(() =>
      expect(api.deleteBookFile).toHaveBeenCalledWith("gastos", PARENT_NAME),
    );

    // El padre virtual desaparece…
    await waitFor(() => expect(screen.queryByText(PARENT_NAME)).toBeNull());
    // …y los hijos NO re-aparecen como filas huérfanas.
    for (const child of CHILD_NAMES) {
      expect(screen.queryByText(child)).toBeNull();
    }
  });
});

describe("buildPendingPreReviewEntries (F7) — conteo coherente", () => {
  it("incluye huérfanos del inbox + el padre del upload; el total == inbox pendiente", () => {
    const orphanChildren: BookFile[] = Array.from({ length: 60 }, (_, i) => ({
      name: `old__${i + 1}of60.pdf`,
      size_kb: 1,
      added: "2026-01-01T00:00:00Z",
      status: "uploaded",
    }));
    const newChildren: BookFile[] = [1, 2, 3].map((i) => ({
      name: `new__${i}of3.pdf`,
      size_kb: 1,
      added: "2026-01-01T00:00:00Z",
      status: "uploaded",
    }));
    const books: Book[] = [
      { id: "gastos", label: "x", folder: "compras", files: [...orphanChildren, ...newChildren] },
      { id: "ingresos", label: "x", folder: "ventas", files: [] },
      { id: "bienes", label: "x", folder: "bienes", files: [] },
    ];
    const merged: Record<string, BookFile[]> = {
      gastos: [
        {
          name: "new.pdf",
          size_kb: 1,
          added: "2026-01-01T00:00:00Z",
          pre_scan: {
            status: "split",
            n_pages: 3,
            detected_invoices: 3,
            children: [1, 2, 3].map((i) => ({
              doc_id: `new__${i}of3`,
              folder_name: "",
              status: "uploaded",
            })),
            error: null,
          },
        },
      ],
    };

    const out = buildPendingPreReviewEntries(books, merged);
    // 60 huérfanos (single) + 1 padre virtual split — los 3 hijos del nuevo
    // split quedan ocultos bajo el padre.
    expect(out.gastos).toHaveLength(61);
    expect(out.gastos.some((f) => f.name === "new__1of3.pdf")).toBe(false);
    const total = out.gastos.reduce((s, f) => s + (f.pre_scan?.detected_invoices ?? 1), 0);
    expect(total).toBe(63);
  });

  it("devuelve vacío si no hay nada pendiente", () => {
    expect(buildPendingPreReviewEntries(null, {})).toEqual({});
  });
});
