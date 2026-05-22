"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  Upload,
  Loader2,
  FileText,
  RefreshCw,
  X,
} from "lucide-react";
import { Book, BookFile, Libro } from "@/lib/types";
import { fetchBooks, uploadFiles, runPipeline, deleteBookFile } from "@/lib/api";

interface BooksManagerProps {
  onPipelineStart: () => void;
  // Activado cuando el usuario clica "Abrir reviewer" en el toast de
  // ``already_processed`` (PDF ya conocido con hijos en estado no-terminal).
  // ``focusDocIds`` se propaga al InvoiceReviewer para posicionarlo en la
  // primera factura pendiente y opcionalmente filtrar la cola.
  onNavigateToReview?: (focusDocIds: string[]) => void;
  // Llamado tras un upload exitoso con resultados del pre-scan. El padre
  // decide si transitar al stage "pre-review" según `detected_summary`.
  onUploadComplete?: (bookId: Libro, files: BookFile[]) => void;
  // Datos de pre-revisión ya conocidos por el padre (sobreviven al unmount
  // de este componente cuando el operario navega a pre-review y vuelve).
  // Si está presente y no está vacío, "Escanear" enruta a pre-review
  // incluso si el accumulator local de esta instancia está vacío.
  parentPreReviewFilesByBook?: Record<string, BookFile[]>;
  // Llamado tras borrar un archivo para que el padre retire la entrada de su
  // snapshot de pre-revisión. Sin esto, un padre virtual de split conservado
  // por el padre (`parentPreReviewFilesByBook`) seguiría visible tras el
  // borrado porque este componente no puede mutar la prop.
  onFileRemoved?: (bookId: Libro, fileName: string) => void;
}

interface UploadProgress {
  bookId: string;
  percent: number;
  fileName: string;
}

interface PendingFile {
  name: string;
  size_kb: number;
}

const ALLOWED_EXTENSIONS = new Set([".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".bmp"]);

// Estados "no escanear de nuevo" desde Gestión: el pipeline los saltaría y
// el operario tiene que resolverlos desde el Reviewer (review/blocked), o
// son terminales contables (done/confirmed), o terminales por error/cancel,
// o requieren acción específica (pre_scan_failed, split).
//
// - confirmed: asiento contable cerrado en Intermega (backend rechaza DELETE).
// - done:      lista para confirmar humano (espera acción en Reviewer).
// - review:    decisión humana pendiente — vive en Reviewer.
// - blocked:   bloqueada por regla fiscal — el operario revisa en Reviewer.
// - cancelled: soft-deleted por usuario.
// - error:     fallo técnico (re-OCR explícito requerido).
// - split:     PDF original ya dividido; sus hijos son los facturables.
// - pre_scan_failed: pre-scan falló; retry/override desde Pre-revisión.
const TERMINAL_STATUSES = new Set([
  "done",
  "confirmed",
  "review",
  "blocked",
  "cancelled",
  "error",
  "split",
  "pre_scan_failed",
]);

export function BooksManager({
  onPipelineStart,
  onNavigateToReview,
  onUploadComplete,
  parentPreReviewFilesByBook,
  onFileRemoved,
}: BooksManagerProps) {
  const [books, setBooks] = useState<Book[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadingBooks, setUploadingBooks] = useState<Set<string>>(new Set());
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [dragOverBook, setDragOverBook] = useState<string | null>(null);
  const [dragFileCount, setDragFileCount] = useState(0);
  const [deletingFile, setDeletingFile] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<Record<string, PendingFile[]>>({});
  // Acumulador de resultados de pre-scan por libro. Se rellena tras cada
  // upload exitoso con `result.files` (que ya trae el sub-objeto `pre_scan`).
  // No dispara navegación: la transición a Pre-revisión sólo ocurre cuando
  // el operario pulsa "Escanear" en el footer.
  const [accumulatedUploads, setAccumulatedUploads] = useState<Record<string, BookFile[]>>({});

  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const uploadingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  }, []);

  const loadBooks = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await fetchBooks();
      if (mountedRef.current) setBooks(response.books);
    } catch {
      if (mountedRef.current) {
        setError("No se pudo conectar con el servidor. Verifica que el backend está funcionando.");
        toast.error("Error cargando libros contables");
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => { loadBooks(); }, [loadBooks]);

  const pollBooksUntilSynced = useCallback(
    async (bookId: string, expectedNames: string[], maxRetries = 3, intervalMs = 800) => {
      for (let attempt = 0; attempt < maxRetries; attempt++) {
        if (!mountedRef.current) return { missing: expectedNames, books: null as Book[] | null };
        try {
          const response = await fetchBooks();
          if (!mountedRef.current) return { missing: expectedNames, books: null };
          const book = response.books.find((b) => b.id === bookId);
          const serverNames = new Set(book?.files.map((f) => f.name) ?? []);
          const stillMissing = expectedNames.filter((n) => !serverNames.has(n));
          if (stillMissing.length === 0) {
            return { missing: [], books: response.books };
          }
          if (attempt < maxRetries - 1) {
            await new Promise((r) => setTimeout(r, intervalMs));
          } else {
            return { missing: stillMissing, books: response.books };
          }
        } catch {
          if (attempt < maxRetries - 1) {
            await new Promise((r) => setTimeout(r, intervalMs));
          } else {
            return { missing: expectedNames, books: null };
          }
        }
      }
      return { missing: expectedNames, books: null };
    },
    []
  );

  const reconcileAfterUpload = useCallback(
    (bookId: string, _uploadedNames: string[], serverBooks: Book[] | null, resultFiles: BookFile[] | undefined) => {
      if (!mountedRef.current) return;

      if (serverBooks) {
        // Usamos serverBooks como verdad (incluye hijos del split que el
        // splitter creó dentro de /upload) y enriquecemos cada archivo con
        // el `pre_scan` que devolvió la respuesta del upload — los hijos
        // viven en /api/books pero su `pre_scan` solo se conoce vía padre
        // (que está en _originales/ y no aparece en /api/books).
        const resultByName = new Map<string, BookFile>();
        for (const f of resultFiles ?? []) resultByName.set(f.name, f);
        setBooks(
          serverBooks.map((b) => {
            if (b.id !== bookId) return b;
            return {
              ...b,
              files: b.files.map((f) => {
                const enriched = resultByName.get(f.name);
                return enriched
                  ? { ...f, pre_scan: enriched.pre_scan ?? f.pre_scan }
                  : f;
              }),
            };
          }),
        );
      } else if (resultFiles && resultFiles.length > 0) {
        // Sin serverBooks (poll falló) — caemos al merge optimista anterior.
        setBooks((prev) =>
          prev
            ? prev.map((b) => {
                if (b.id !== bookId) return b;
                const existingNames = new Set(b.files.map((f) => f.name));
                const newFiles = resultFiles.filter((f) => !existingNames.has(f.name));
                return { ...b, files: [...b.files, ...newFiles] };
              })
            : prev,
        );
      }

      // Borra TODOS los pending del bookId. El backend puede renombrar archivos
      // por colisión (ej. ``5.pdf`` → ``5_178xxx.pdf``), con lo que el nombre
      // original ya no matchea ``result.uploaded`` y el spinner "Subiendo..."
      // quedaba indefinido. La fuente de verdad post-upload es ``serverBooks``
      // / ``resultFiles``, no la lista pendiente.
      setPendingFiles((prev) => {
        if (!(bookId in prev)) return prev;
        const next = { ...prev };
        delete next[bookId];
        return next;
      });
    },
    []
  );

  const handleDrop = useCallback(
    async (bookId: string, droppedFiles: File[]) => {
      setDragOverBook(null);
      setDragFileCount(0);

      if (uploadingRef.current) {
        toast.error("Espera a que termine la subida actual antes de añadir más archivos.");
        return;
      }
      uploadingRef.current = true;

      const pendingEntries: PendingFile[] = droppedFiles.map((f) => ({
        name: f.name,
        size_kb: Math.round((f.size / 1024) * 10) / 10,
      }));

      setPendingFiles((prev) => ({
        ...prev,
        [bookId]: [...(prev[bookId] || []), ...pendingEntries],
      }));

      setUploadingBooks((prev) => new Set(prev).add(bookId));
      // El backend hace pre-scan síncrono Fase 1 (Gemini Vision) por PDF,
      // así que la subida puede tardar varios segundos por archivo multi-página.
      // Mostramos un mensaje explícito para que el operario no piense que está
      // colgado.
      const hasPdf = droppedFiles.some((f) => f.name.toLowerCase().endsWith(".pdf"));
      setUploadProgress({
        bookId,
        percent: 0,
        fileName: hasPdf
          ? `Subiendo y analizando ${droppedFiles.length} archivo(s)…`
          : `${droppedFiles.length} archivo(s)`,
      });

      try {
        const result = await uploadFiles(
          bookId as "gastos" | "ingresos" | "bienes",
          droppedFiles
        );

        if (!mountedRef.current) return;

        if (result.errors.length > 0) {
          result.errors.forEach((e) => toast.error(`${e.file}: ${e.error}`));
        }

        // PDFs ya conocidos cuyos hijos siguen pendientes de revisión.
        // Informamos al operario pero NO ofrecemos "Abrir reviewer": el
        // acceso al Reviewer desde Gestión está intencionalmente cerrado para
        // mantener el workspace limpio tras un Nuevo escaneo.
        const alreadyProcessed = result.already_processed ?? [];
        if (alreadyProcessed.length > 0) {
          for (const info of alreadyProcessed) {
            const n = info.pending.length;
            const facturas = n === 1 ? "factura pendiente" : "facturas pendientes";
            toast.info(
              `${info.file}: ya procesado. ${n} ${facturas} de revisión.`,
              { duration: 8000 },
            );
          }
        }

        const uploadedCount = result.uploaded.length;
        if (
          uploadedCount === 0 &&
          (result.errors.length > 0 || alreadyProcessed.length > 0)
        ) {
          setPendingFiles((prev) => {
            const next = { ...prev };
            delete next[bookId];
            return next;
          });
          setUploadProgress(null);
          if (alreadyProcessed.length > 0 && result.errors.length === 0) {
            // Caso limpio: TODO el lote eran PDFs ya conocidos con pendientes.
            // El toast info ya está mostrado; no añadimos toast de error.
          } else {
            toast.error("Ningún archivo pudo subirse. Revisa los errores.");
          }
          return;
        }

        setUploadProgress({ bookId, percent: 30, fileName: "Sincronizando con el servidor…" });

        // Si un PDF se splitteó, el original se mueve a `_originales/` y los
        // hijos aparecen en el inbox con sufijo ``__iofN.pdf``. Esperamos los
        // hijos (no el padre) para no marcar "missing" el original que ya no
        // está visible.
        const expectedNames = result.uploaded.flatMap((name) => {
          const info = result.files?.find((f) => f.name === name);
          if (info?.pre_scan?.status === "split" && info.pre_scan.children.length > 0) {
            return info.pre_scan.children.map((c) => `${c.doc_id}.pdf`);
          }
          return [name];
        });
        const { missing, books: serverBooks } = await pollBooksUntilSynced(
          bookId,
          expectedNames
        );

        if (!mountedRef.current) return;

        setUploadProgress({ bookId, percent: 100, fileName: "" });

        reconcileAfterUpload(bookId, result.uploaded, serverBooks, result.files);

        if (uploadedCount > 0) {
          if (missing.length > 0) {
            toast.warning(
              `${uploadedCount} subido(s). ${missing.length} aún no visible(s) — usa "Recargar" si no aparece(n).`
            );
          } else {
            toast.success(`${uploadedCount} archivo(s) subido(s) a ${bookId}`);
          }
        }

        setTimeout(() => {
          if (!mountedRef.current) return;
          setUploadProgress((prev) => (prev?.bookId === bookId ? null : prev));
        }, 600);

        // Acumulamos los resultados con `pre_scan` para pasárselos al stage
        // Pre-revisión, pero NO transicionamos automáticamente. El operario
        // pulsará "Escanear" para confirmar el paso (ver handleRunPipeline).
        if (
          result.files &&
          result.files.length > 0 &&
          (result.detected_summary?.total_invoices ?? 0) > 0
        ) {
          const uploadedNames = new Set(result.uploaded);
          const newFiles = result.files.filter((f) => uploadedNames.has(f.name));
          if (newFiles.length > 0) {
            setAccumulatedUploads((prev) => {
              const existing = prev[bookId] || [];
              const byName = new Map(existing.map((f) => [f.name, f] as const));
              for (const f of newFiles) byName.set(f.name, f);
              return { ...prev, [bookId]: [...byName.values()] };
            });
          }
        }
      } catch (err) {
        if (!mountedRef.current) return;
        const message = err instanceof Error ? err.message : String(err);
        if (message === "PIPELINE_RUNNING") {
          toast.warning(
            "Hay un procesado en curso. Espera a que termine para subir nuevas facturas."
          );
        } else if (message.includes("timed out") || message.includes("AbortError")) {
          toast.error("Timeout al subir archivos. El servidor puede estar sobrecargado.");
        } else {
          toast.error(`Error de red al subir archivos: ${message}`);
        }
        setUploadProgress(null);
        setPendingFiles((prev) => {
          const next = { ...prev };
          delete next[bookId];
          return next;
        });
      } finally {
        uploadingRef.current = false;
        setUploadingBooks((prev) => {
          const next = new Set(prev);
          next.delete(bookId);
          return next;
        });
      }
    },
    [pollBooksUntilSynced, reconcileAfterUpload]
  );

  const handleDragOver = useCallback((e: React.DragEvent, bookId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setDragOverBook(bookId);
    if (e.dataTransfer.files.length > 0) {
      setDragFileCount(e.dataTransfer.files.length);
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    const { clientX, clientY } = e;
    if (
      clientX <= rect.left ||
      clientX >= rect.right ||
      clientY <= rect.top ||
      clientY >= rect.bottom
    ) {
      setDragOverBook(null);
      setDragFileCount(0);
    }
  }, []);

  const handleDropEvent = useCallback(
    (e: React.DragEvent, bookId: string) => {
      e.preventDefault();
      setDragFileCount(0);
      const files = Array.from(e.dataTransfer.files);
      if (files.length === 0) return;
      const valid = files.filter((f) => {
        const ext = "." + (f.name.split(".").pop()?.toLowerCase() ?? "");
        return ALLOWED_EXTENSIONS.has(ext);
      });
      const invalid = files.filter((f) => {
        const ext = "." + (f.name.split(".").pop()?.toLowerCase() ?? "");
        return !ALLOWED_EXTENSIONS.has(ext);
      });
      if (invalid.length > 0) {
        invalid.forEach((f) => toast.error(`Tipo no soportado: ${f.name}`));
      }
      if (valid.length > 0) handleDrop(bookId, valid);
    },
    [handleDrop]
  );

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>, bookId: string) => {
      const files = Array.from(e.target.files || []);
      if (files.length > 0) handleDrop(bookId, files);
      e.target.value = "";
    },
    [handleDrop]
  );

  const handleDeleteFile = useCallback(async (bookId: string, filename: string) => {
    const key = `${bookId}/${filename}`;
    setDeletingFile(key);
    try {
      await deleteBookFile(bookId as "gastos" | "ingresos" | "bienes", filename);
      if (!mountedRef.current) return;
      // Si `filename` es un padre de split, el backend borra el grupo entero
      // (original + hijos `{stem}__iofN.pdf`). El padre virtual no está en
      // `b.files` (sólo sus hijos, ocultos), así que además de quitar el
      // propio archivo retiramos sus hijos para que no re-aparezcan como
      // filas huérfanas al desaparecer el stem del acumulador.
      const stem = filename.replace(/\.[^.]+$/, "");
      const childRe = new RegExp(
        `^${stem.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}__\\d+of\\d+\\.pdf$`,
        "i",
      );
      setBooks((prev) =>
        prev
          ? prev.map((b) =>
              b.id === bookId
                ? {
                    ...b,
                    files: b.files.filter(
                      (f: BookFile) => f.name !== filename && !childRe.test(f.name),
                    ),
                  }
                : b
            )
          : prev
      );
      // Si el archivo borrado estaba en el acumulador de pre-scan, retirarlo.
      setAccumulatedUploads((prev) => {
        if (!(bookId in prev)) return prev;
        const filtered = prev[bookId].filter((f) => f.name !== filename);
        if (filtered.length === prev[bookId].length) return prev;
        const next = { ...prev };
        if (filtered.length === 0) delete next[bookId];
        else next[bookId] = filtered;
        return next;
      });
      // El padre virtual de un split puede vivir en el snapshot del padre
      // (`parentPreReviewFilesByBook`), que esta instancia no puede mutar.
      onFileRemoved?.(bookId as Libro, filename);
      toast.success(`Archivo eliminado: ${filename}`);
    } catch (err) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Error eliminando archivo: ${message}`);
    } finally {
      if (mountedRef.current) setDeletingFile(null);
    }
  }, [onFileRemoved]);

  // Total real de facturas detectadas. Mergeamos accumulator local + datos del
  // padre (que sobreviven a un round-trip pre-review → volver a Gestión).
  // El merge es por nombre de archivo dentro de cada libro — los archivos
  // recién subidos pisan a versiones anteriores con el mismo nombre.
  const mergedFilesByBook: Record<string, BookFile[]> = (() => {
    const out: Record<string, BookFile[]> = {};
    const allBookIds = new Set([
      ...Object.keys(parentPreReviewFilesByBook || {}),
      ...Object.keys(accumulatedUploads),
    ]);
    for (const bid of allBookIds) {
      const byName = new Map<string, BookFile>();
      for (const f of parentPreReviewFilesByBook?.[bid] || []) byName.set(f.name, f);
      for (const f of accumulatedUploads[bid] || []) byName.set(f.name, f);
      out[bid] = [...byName.values()];
    }
    return out;
  })();

  const totalDetectedInvoices = Object.values(mergedFilesByBook).reduce(
    (sum, files) =>
      sum +
      files.reduce((s, f) => s + (f.pre_scan?.detected_invoices ?? 1), 0),
    0,
  );

  const handleRunPipeline = useCallback(async () => {
    // Si hay resultados de pre-scan disponibles (locales o conservados por el
    // padre desde una sesión anterior), vamos al stage Pre-revisión para
    // confirmar el desglose. El pipeline lo lanza el botón "Escanear" de
    // PreReviewStage.
    const mergedBookIds = Object.keys(mergedFilesByBook);
    if (onUploadComplete && mergedBookIds.length > 0) {
      for (const bid of mergedBookIds) {
        const files = mergedFilesByBook[bid];
        if (files && files.length > 0) {
          onUploadComplete(bid as Libro, files);
        }
      }
      return;
    }

    // Fallback (sin uploads recientes con pre-scan): pipeline directo,
    // sin pasar por Pre-revisión. Mantiene el flujo legacy para PDFs ya
    // presentes en el inbox de sesiones anteriores.
    try {
      const result = await runPipeline();
      toast.success(`Pipeline iniciado: ${result.total} archivo(s) a procesar`);
      onPipelineStart();
    } catch (err) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : String(err);
      if (message.includes("PIPELINE_ALREADY_RUNNING")) {
        toast.warning("El pipeline ya está en ejecución");
        onPipelineStart();
      } else {
        toast.error(`Error iniciando pipeline: ${message}`);
      }
    }
  }, [mergedFilesByBook, onUploadComplete, onPipelineStart]);

  const isPendingFile = (f: BookFile) =>
    !f.status || !TERMINAL_STATUSES.has(f.status);

  // Vista derivada por libro: oculta los hijos `__iofN.pdf` cuyo padre ya
  // está cubierto por el pre-scan (acumulador) y añade el padre virtual con
  // su badge "N facturas". Resultado: 1 fila por upload, no N filas por split.
  const splitParentStemsByBook: Record<string, Set<string>> = (() => {
    const out: Record<string, Set<string>> = {};
    for (const [bid, files] of Object.entries(mergedFilesByBook)) {
      const stems = new Set<string>();
      for (const f of files) {
        if (f.pre_scan?.status === "split") {
          stems.add(f.name.replace(/\.[^.]+$/, ""));
        }
      }
      out[bid] = stems;
    }
    return out;
  })();
  const isChildOfKnownParent = (bookId: string, fileName: string) => {
    const stems = splitParentStemsByBook[bookId];
    if (!stems || stems.size === 0) return false;
    const m = fileName.match(/^(.+?)__\d+of\d+\.pdf$/i);
    return m ? stems.has(m[1]) : false;
  };

  const displayedFilesByBook: Record<string, BookFile[]> = (() => {
    const out: Record<string, BookFile[]> = {};
    for (const b of books ?? []) {
      const knownNames = new Set(b.files.map((f) => f.name));
      const visible = b.files.filter(
        (f) => isPendingFile(f) && !isChildOfKnownParent(b.id, f.name),
      );
      // Injecta padres virtuales del acumulador que ya no están en el inbox
      // (típicamente porque el splitter los movió a `_originales/`).
      const virtualParents = (mergedFilesByBook[b.id] ?? []).filter(
        (f) => !knownNames.has(f.name) && f.pre_scan?.status === "split",
      );
      out[b.id] = [...visible, ...virtualParents];
    }
    return out;
  })();

  // Conteo del CTA: suma de facturas detectadas en los padres del acumulador
  // + archivos pendientes que no están cubiertos por ningún padre (stale del
  // inbox, imágenes recién subidas, etc.).
  let ctaCount = 0;
  for (const b of books ?? []) {
    for (const f of b.files) {
      if (!isPendingFile(f)) continue;
      if (isChildOfKnownParent(b.id, f.name)) continue; // contado en el padre
      // Pre-scan info conocido para este archivo concreto (no padre):
      const fromAcc = mergedFilesByBook[b.id]?.find((a) => a.name === f.name);
      const detected = fromAcc?.pre_scan?.detected_invoices;
      ctaCount += detected ?? 1;
    }
    // Padres virtuales (no en inbox pero sí en acumulador).
    for (const f of mergedFilesByBook[b.id] ?? []) {
      const inInbox = b.files.some((bf) => bf.name === f.name);
      if (inInbox) continue;
      if (f.pre_scan?.status === "split") {
        ctaCount += f.pre_scan.detected_invoices;
      } else if (f.pre_scan?.status === "single") {
        ctaCount += 1;
      }
    }
  }
  const totalDisplayedFiles = Object.values(displayedFilesByBook).reduce(
    (sum, files) => sum + files.length,
    0,
  );

  if (loading && !books) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
          <p className="text-sm text-slate-500">Cargando libros contables...</p>
        </div>
      </div>
    );
  }

  if (error && !books) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">{error}</p>
          <Button onClick={loadBooks} variant="outline" className="text-xs rounded-none uppercase tracking-[0.15em]">
            Reintentar
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-white">
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0">
        <div className="flex items-center gap-4">
          <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">
            Gestión de Facturas
          </h2>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-bold text-slate-400 font-mono">
            {totalDisplayedFiles} archivo(s) pendiente(s)
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={loadBooks}
            disabled={loading}
            className="text-[10px] uppercase tracking-[0.15em] text-slate-400"
            aria-label="Recargar lista de archivos"
          >
            <RefreshCw className={cn("h-3 w-3 mr-1", loading && "animate-spin")} />
            Recargar
          </Button>
        </div>
      </header>

      <ScrollArea className="flex-1">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-6">
          {books?.map((book) => {
            const isUploading = uploadingBooks.has(book.id);
            const isDragOver = dragOverBook === book.id;
            const progress = uploadProgress?.bookId === book.id ? uploadProgress.percent : 0;
            const visibleFiles = displayedFilesByBook[book.id] ?? [];
            const bookPending = pendingFiles[book.id] || [];
            const hasPending = bookPending.length > 0;
            const showPlaceholder = visibleFiles.length === 0 && !isUploading && !hasPending;

            return (
              <div
                key={book.id}
                className={cn(
                  "border rounded-lg p-4 transition-all relative",
                  isDragOver && "border-teal-400 bg-teal-50/30 border-dashed",
                  !isDragOver && "border-slate-200 bg-white"
                )}
                onDragOver={(e) => handleDragOver(e, book.id)}
                onDragLeave={handleDragLeave}
                onDrop={(e) => handleDropEvent(e, book.id)}
              >
                <h3 className="text-xs font-black uppercase tracking-[0.1em] text-slate-700 mb-3">
                  {book.label}
                </h3>

                {isDragOver && dragFileCount > 0 && (
                  <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-10">
                    <div className="bg-teal-500 text-white text-xs font-bold px-4 py-2 rounded-lg shadow-lg animate-in fade-in zoom-in duration-150">
                      +{dragFileCount} archivo{dragFileCount !== 1 ? "s" : ""}
                    </div>
                  </div>
                )}

                {showPlaceholder ? (
                  <div
                    className={cn(
                      "border-2 border-dashed rounded-lg p-8 text-center transition-all cursor-pointer",
                      isDragOver
                        ? "border-teal-400 bg-teal-50/50"
                        : "border-slate-200 hover:border-slate-300"
                    )}
                    onClick={() => fileInputRefs.current[book.id]?.click()}
                  >
                    <Upload className="h-8 w-8 mx-auto mb-2 text-slate-300" />
                    <p className="text-xs text-slate-400 mb-1">Arrastra archivos aquí</p>
                    <p className="text-[10px] text-slate-300">o haz clic para seleccionar</p>
                    <input
                      ref={(el) => { fileInputRefs.current[book.id] = el; }}
                      type="file"
                      multiple
                      accept=".pdf,.jpg,.jpeg,.png,.tiff,.tif,.webp,.bmp"
                      className="hidden"
                      onChange={(e) => handleFileSelect(e, book.id)}
                    />
                  </div>
                ) : (
                  <div className="space-y-2">
                    {isUploading && uploadProgress?.bookId === book.id && (
                      <div className="mb-3 space-y-1">
                        <div className="flex items-center justify-between text-[10px] font-mono">
                          <span className="text-teal-600 truncate max-w-[200px]">
                            {uploadProgress.fileName || "Subiendo..."}
                          </span>
                          <span className="text-teal-400 tabular-nums">{progress}%</span>
                        </div>
                        {progress > 0 && progress < 100 ? (
                          <Progress value={progress} className="h-1.5 bg-slate-100 [&>div]:bg-teal-500" />
                        ) : (
                          <div className="relative h-1.5 w-full bg-slate-100 overflow-hidden rounded-full">
                            <div
                              className="absolute top-0 left-0 right-0 bottom-0"
                              style={{
                                background: "linear-gradient(90deg, transparent, hsl(173 80% 40%), transparent)",
                                animation: "indeterminate 1.5s ease-in-out infinite",
                              }}
                            />
                            <style>{`@keyframes indeterminate { 0% { transform: translateX(-100%); } 50% { transform: translateX(100%); } 100% { transform: translateX(-100%); } }`}</style>
                          </div>
                        )}
                      </div>
                    )}

                    {bookPending.map((pf, idx) => (
                      <div
                        key={`pending-${book.id}-${pf.name}`}
                        className="flex items-center gap-2 p-2 bg-teal-50/70 rounded border border-teal-100 animate-in fade-in slide-in-from-top-1 duration-200"
                        style={{ animationDelay: `${idx * 50}ms`, animationFillMode: "backwards" }}
                      >
                        <Loader2 className="h-4 w-4 text-teal-500 animate-spin flex-shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-mono truncate text-teal-800">{pf.name}</p>
                          <p className="text-[10px] text-teal-500">Subiendo…</p>
                        </div>
                      </div>
                    ))}

                    {visibleFiles.map((file: BookFile) => {
                      const fileKey = `${book.id}/${file.name}`;
                      const isDeleting = deletingFile === fileKey;
                      const isFresh = bookPending.some((pf) => pf.name === file.name) === false;
                      // Si el archivo acaba de ser pre-scaneado en esta sesión,
                      // mostramos el desglose detectado. Buscamos primero en el
                      // merge (local + heredado del padre) y, en último caso,
                      // en el propio file.pre_scan (sólo presente en respuestas
                      // de /upload, no en /api/books).
                      const accFile = mergedFilesByBook[book.id]?.find((f) => f.name === file.name);
                      const preScan = accFile?.pre_scan ?? file.pre_scan ?? null;
                      const detectedBadge =
                        preScan && preScan.status === "split"
                          ? `${preScan.detected_invoices} facturas`
                          : preScan && preScan.status === "failed"
                          ? "pre-scan ko"
                          : null;
                      const statusLabel = file.status
                        ? file.status === "done" ? "procesada"
                        : file.status === "review" ? "revisar"
                        : file.status === "blocked" ? "bloqueada"
                        : file.status === "error" ? "error"
                        : file.status === "processing" ? "procesando…"
                        : file.status === "pre_scan_failed" ? "pre-scan ko"
                        : file.status === "split" ? "dividida"
                        : null
                        : null;
                      const statusClass = file.status === "done"
                        ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                        : file.status === "review"
                        ? "bg-amber-50 text-amber-700 border-amber-200"
                        : file.status === "blocked" || file.status === "error" || file.status === "pre_scan_failed"
                        ? "bg-red-50 text-red-700 border-red-200"
                        : file.status === "split"
                        ? "bg-violet-50 text-violet-700 border-violet-200"
                        : "bg-slate-50 text-slate-600 border-slate-200";
                      return (
                        <div
                          key={fileKey}
                          className={cn(
                            "flex items-center gap-2 p-2 bg-slate-50 rounded border border-slate-100 group/file",
                            isFresh && "animate-in fade-in slide-in-from-top-1 duration-200"
                          )}
                        >
                          <FileText className="h-4 w-4 text-slate-400 flex-shrink-0" />
                          <div className="flex-1 min-w-0">
                            <p className="text-xs font-mono truncate">{file.name}</p>
                            <p className="text-[10px] text-slate-400">{file.size_kb} KB</p>
                          </div>
                          {detectedBadge && (
                            <span
                              className={cn(
                                "text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border",
                                preScan?.status === "failed"
                                  ? "bg-red-50 text-red-700 border-red-200"
                                  : "bg-violet-50 text-violet-700 border-violet-200",
                              )}
                              title={
                                preScan?.status === "split"
                                  ? `${preScan.n_pages} páginas → ${preScan.detected_invoices} facturas detectadas`
                                  : preScan?.error?.message ?? undefined
                              }
                            >
                              {detectedBadge}
                            </span>
                          )}
                          {statusLabel && (
                            <span
                              className={cn(
                                "text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border",
                                statusClass,
                              )}
                              title={file.folder_name ?? undefined}
                            >
                              {statusLabel}
                            </span>
                          )}
                          <button
                            onClick={() => handleDeleteFile(book.id, file.name)}
                            disabled={isDeleting}
                            className="opacity-0 group-hover/file:opacity-100 transition-opacity p-1 rounded hover:bg-red-50 text-slate-300 hover:text-red-400 disabled:opacity-50"
                            aria-label={`Eliminar ${file.name}`}
                          >
                            {isDeleting ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <X className="h-3 w-3" />
                            )}
                          </button>
                        </div>
                      );
                    })}

                    {!isUploading && (
                      <button
                        className="w-full py-2 text-xs text-slate-400 border border-dashed border-slate-200 rounded hover:border-slate-300 transition-colors"
                        onClick={() => fileInputRefs.current[book.id]?.click()}
                      >
                        + Añadir más archivos
                      </button>
                    )}
                    <input
                      ref={(el) => { fileInputRefs.current[book.id] = el; }}
                      type="file"
                      multiple
                      accept=".pdf,.jpg,.jpeg,.png,.tiff,.tif,.webp,.bmp"
                      className="hidden"
                      onChange={(e) => handleFileSelect(e, book.id)}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </ScrollArea>

      <footer className="h-24 flex items-center justify-center gap-6 px-8 bg-white border-t flex-shrink-0">
        <Button
          size="lg"
          onClick={handleRunPipeline}
          disabled={ctaCount === 0}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            ctaCount === 0 && "opacity-50 cursor-not-allowed"
          )}
        >
          {ctaCount === 0 ? "Sin archivos pendientes" : <>Escanear {ctaCount} Factura(s) →</>}
        </Button>
      </footer>
    </div>
  );
}
