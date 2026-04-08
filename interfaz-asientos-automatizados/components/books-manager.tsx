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
} from "lucide-react";
import { Book, BookFile } from "@/lib/types";
import { fetchBooks, uploadFiles, runPipeline } from "@/lib/api";

interface BooksManagerProps {
  onPipelineStart: () => void;
}

interface UploadProgress {
  bookId: string;
  percent: number;
  fileName: string;
}

export function BooksManager({ onPipelineStart }: BooksManagerProps) {
  const [books, setBooks] = useState<Book[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadingBooks, setUploadingBooks] = useState<Set<string>>(new Set());
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);
  const [dragOverBook, setDragOverBook] = useState<string | null>(null);

  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const loadBooks = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await fetchBooks();
      setBooks(response.books);
    } catch {
      setError("No se pudo conectar con el servidor. Verifica que el backend está funcionando.");
      toast.error("Error cargando libros contables");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadBooks(); }, [loadBooks]);

  const handleDrop = useCallback(
    async (bookId: string, droppedFiles: File[]) => {
      setDragOverBook(null);
      setUploadingBooks((prev) => new Set(prev).add(bookId));
      setUploadProgress({ bookId, percent: 0, fileName: `${droppedFiles.length} archivo(s)` });

      try {
        const result = await uploadFiles(bookId as "gastos" | "ingresos" | "bienes", droppedFiles);

        setUploadProgress({ bookId, percent: 90, fileName: "" });
        await loadBooks();
        setUploadProgress({ bookId, percent: 100, fileName: "" });

        if (result.uploaded.length > 0) {
          toast.success(`${result.uploaded.length} archivo(s) subido(s) a ${bookId}`);
        }
        if (result.errors.length > 0) {
          result.errors.forEach((e) => toast.error(`${e.file}: ${e.error}`));
        }

        setTimeout(() => setUploadProgress((prev) => prev?.bookId === bookId ? null : prev), 600);
      } catch {
        toast.error("Error subiendo archivos");
        setUploadProgress(null);
      } finally {
        setUploadingBooks((prev) => {
          const next = new Set(prev);
          next.delete(bookId);
          return next;
        });
      }
    },
    [loadBooks]
  );

  const handleDragOver = useCallback((e: React.DragEvent, bookId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setDragOverBook(bookId);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOverBook(null);
  }, []);

  const handleDropEvent = useCallback(
    (e: React.DragEvent, bookId: string) => {
      e.preventDefault();
      const files = Array.from(e.dataTransfer.files);
      if (files.length > 0) handleDrop(bookId, files);
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

  const handleRunPipeline = useCallback(async () => {
    try {
      const result = await runPipeline();
      toast.success(`Pipeline iniciado: ${result.total} archivo(s) a procesar`);
      onPipelineStart();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (message.includes("PIPELINE_ALREADY_RUNNING")) {
        toast.warning("El pipeline ya está en ejecución");
        onPipelineStart();
      } else {
        toast.error(`Error iniciando pipeline: ${message}`);
      }
    }
  }, [onPipelineStart]);

  const totalFiles = books?.reduce((sum, b) => sum + b.files.length, 0) ?? 0;

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
      {/* Header */}
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
          {/* Reload books from server */}
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

      {/* Content: 3 columns */}
      <ScrollArea className="flex-1">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-6">
          {books?.map((book) => {
            const isUploading = uploadingBooks.has(book.id);
            const isDragOver = dragOverBook === book.id;
            const progress = uploadProgress?.bookId === book.id ? uploadProgress.percent : 0;

            return (
              <div
                key={book.id}
                className={cn(
                  "border rounded-lg p-4 transition-all",
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

                {book.files.length === 0 && !isUploading ? (
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
                          <span className="text-teal-600 truncate max-w-[150px]">
                            {uploadProgress.fileName || "Subiendo..."}
                          </span>
                        </div>
                        {progress > 0 && progress < 100 ? (
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
                        ) : (
                          <Progress value={progress} className="h-1.5 bg-slate-100" />
                        )}
                      </div>
                    )}

                    {book.files.map((file: BookFile) => (
                      <div
                        key={file.name}
                        className="flex items-center gap-2 p-2 bg-slate-50 rounded border border-slate-100"
                      >
                        <FileText className="h-4 w-4 text-slate-400 flex-shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-mono truncate">{file.name}</p>
                          <p className="text-[10px] text-slate-400">{file.size_kb} KB</p>
                        </div>
                        {/* Delete button removed until backend endpoint is implemented */}
                      </div>
                    ))}

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

      {/* Footer: Run pipeline button */}
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
