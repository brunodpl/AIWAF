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
import { Book, BookFile } from "@/lib/types";
import { fetchBooks, uploadFiles, runPipeline, deleteBookFile } from "@/lib/api";

interface BooksManagerProps {
  onPipelineStart: () => void;
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

export function BooksManager({ onPipelineStart }: BooksManagerProps) {
  const [books, setBooks] = useState<Book[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadingBooks, setUploadingBooks] = useState<Set<string>>(new Set());
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [dragOverBook, setDragOverBook] = useState<string | null>(null);
  const [dragFileCount, setDragFileCount] = useState(0);
  const [deletingFile, setDeletingFile] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<Record<string, PendingFile[]>>({});

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

      if (resultFiles && resultFiles.length > 0) {
        setBooks((prev) =>
          prev
            ? prev.map((b) => {
                if (b.id !== bookId) return b;
                const existingNames = new Set(b.files.map((f) => f.name));
                const newFiles = resultFiles.filter((f) => !existingNames.has(f.name));
                return { ...b, files: [...b.files, ...newFiles] };
              })
            : prev
        );
      } else if (serverBooks) {
        setBooks(serverBooks);
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
      setUploadProgress({
        bookId,
        percent: 0,
        fileName: `${droppedFiles.length} archivo(s)`,
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

        const uploadedCount = result.uploaded.length;
        if (uploadedCount === 0 && result.errors.length > 0) {
          setPendingFiles((prev) => {
            const next = { ...prev };
            delete next[bookId];
            return next;
          });
          setUploadProgress(null);
          toast.error("Ningún archivo pudo subirse. Revisa los errores.");
          return;
        }

        setUploadProgress({ bookId, percent: 30, fileName: "Sincronizando con el servidor…" });

        const { missing, books: serverBooks } = await pollBooksUntilSynced(
          bookId,
          result.uploaded
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
      } catch (err) {
        if (!mountedRef.current) return;
        const message = err instanceof Error ? err.message : String(err);
        if (message.includes("timed out") || message.includes("AbortError")) {
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
      setBooks((prev) =>
        prev
          ? prev.map((b) =>
              b.id === bookId
                ? { ...b, files: b.files.filter((f: BookFile) => f.name !== filename) }
                : b
            )
          : prev
      );
      toast.success(`Archivo eliminado: ${filename}`);
    } catch (err) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Error eliminando archivo: ${message}`);
    } finally {
      if (mountedRef.current) setDeletingFile(null);
    }
  }, []);

  const handleRunPipeline = useCallback(async () => {
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
  }, [onPipelineStart]);

  const isPendingFile = (f: BookFile) => f.status !== "done";
  const totalFiles =
    books?.reduce((sum, b) => sum + b.files.filter(isPendingFile).length, 0) ?? 0;

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
            {totalFiles} archivo(s) pendiente(s)
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
            const visibleFiles = book.files.filter(isPendingFile);
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
                      const statusLabel = file.status
                        ? file.status === "done" ? "procesada"
                        : file.status === "review" ? "revisar"
                        : file.status === "blocked" ? "bloqueada"
                        : file.status === "error" ? "error"
                        : file.status === "processing" ? "procesando…"
                        : null
                        : null;
                      const statusClass = file.status === "done"
                        ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                        : file.status === "review"
                        ? "bg-amber-50 text-amber-700 border-amber-200"
                        : file.status === "blocked" || file.status === "error"
                        ? "bg-red-50 text-red-700 border-red-200"
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
          disabled={totalFiles === 0}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            totalFiles === 0 && "opacity-50 cursor-not-allowed"
          )}
        >
          {totalFiles === 0 ? "Sin archivos pendientes" : <>Escanear {totalFiles} Factura(s) →</>}
        </Button>
      </footer>
    </div>
  );
}
