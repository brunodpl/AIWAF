"use client";

import { useState, useCallback, useEffect } from "react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { StageIndicator } from "@/components/stage-indicator";
import { BooksManager } from "@/components/books-manager";
import {
  PreReviewStage,
  type PreReviewUploadEntry,
} from "@/components/pre-review-stage";
import { PipelineProgress } from "@/components/pipeline-progress";
import { InvoiceReviewer } from "@/components/invoice-reviewer";
import { ExportStage } from "@/components/export-stage";
import { FeedbackButton } from "@/components/feedback-button";
import { UpdateBanner } from "@/components/update-banner";
import { HistorialOverlay } from "@/components/historial-overlay";
import {
  ApprovedInvoiceData,
  BookFile,
  FiscalLine,
  Libro,
} from "@/lib/types";
import { fetchInvoices, resetPipeline } from "@/lib/api";

type Stage = "books" | "pre-review" | "processing" | "review" | "export";

const STORAGE_KEY_INVOICES = "horeca_approved_invoices";
const STORAGE_KEY_STAGE = "horeca_current_stage";

export default function Home() {
  const [hydrated, setHydrated] = useState(false);
  const [stage, setStage] = useState<Stage>("books");
  const [historialOpen, setHistorialOpen] = useState(false);
  const [approvedInvoices, setApprovedInvoices] = useState<Map<string, ApprovedInvoiceData>>(new Map());
  const [rejectedInvoices, setRejectedInvoices] = useState<Set<string>>(new Set());

  // Restore from localStorage only after mount (client-side)
  useEffect(() => {
    let cancelled = false;
    async function restore() {
      try {
        const savedInvoices = localStorage.getItem(STORAGE_KEY_INVOICES);
        if (savedInvoices) {
          const entries = JSON.parse(savedInvoices) as [string, ApprovedInvoiceData][];
          if (entries.length > 0) {
            let validEntries = entries;
            try {
              const response = await fetchInvoices();
              const backendIds = new Set(response.invoices.map((i: { id: string }) => i.id));
              validEntries = entries.filter(([id]) => backendIds.has(id));
            } catch {
              // Backend no disponible — restaurar de localStorage (offline-first)
            }

            if (validEntries.length > 0 && !cancelled) {
              setApprovedInvoices(new Map(validEntries));
              const savedStage = localStorage.getItem(STORAGE_KEY_STAGE);
              if (savedStage && ["review", "export"].includes(savedStage)) {
                setStage(savedStage as Stage);
              } else {
                setStage("review");
              }
            }
          }
        }
      } catch { /* ignore corrupt data */ }
      if (!cancelled) setHydrated(true);
    }
    restore();
    return () => { cancelled = true; };
  }, []);

  // Persist approvedInvoices to localStorage (debounced)
  useEffect(() => {
    if (!hydrated) return;
    const timer = setTimeout(() => {
      localStorage.setItem(
        STORAGE_KEY_INVOICES,
        JSON.stringify(Array.from(approvedInvoices.entries()))
      );
    }, 500);
    return () => clearTimeout(timer);
  }, [approvedInvoices, hydrated]);

  // Persist stage to localStorage
  useEffect(() => {
    if (!hydrated) return;
    localStorage.setItem(STORAGE_KEY_STAGE, stage);
  }, [stage, hydrated]);

  const [resetting, setResetting] = useState(false);
  const [booksNonce, setBooksNonce] = useState(0);
  const [totalQueued, setTotalQueued] = useState<number>(0);
  // Snapshot de los uploads que llevan al stage `pre-review`. Cada entrada
  // mantiene el `pre_scan` por archivo devuelto por POST /upload — sólo en
  // memoria; si el operario refresca la página, vuelve a "books" y los
  // archivos siguen visibles allí con su `status` (uploaded/pre_scan_failed
  // /split/...) desde /api/books.
  const [preReviewEntries, setPreReviewEntries] = useState<PreReviewUploadEntry[]>([]);
  // doc_ids a enfocar en el reviewer (set por BooksManager cuando el usuario
  // resube un PDF ya conocido con hijos pendientes — ver toast "Abrir reviewer").
  const [reviewFocusIds, setReviewFocusIds] = useState<string[] | undefined>(undefined);
  // "Nuevo escaneo" = empezar un lote limpio. Llama a /api/pipeline/reset
  // para descartar asientos pendientes (status != done) que se quedarían
  // fantasma en /review tras un escaneo abortado. ATENCIÓN: el reset
  // también borra los splits del inbox (libros/facturas/*__NofN.pdf) — los
  // PDFs originales no-split sobreviven, los hijos del splitter NO. Si el
  // backend falla, seguimos limpiando estado UI (defensa en profundidad).
  const handleReset = useCallback(async () => {
    setResetting(true);
    try {
      await resetPipeline();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.warning(`Reset backend falló: ${msg}. UI se limpia igualmente.`);
    }
    setApprovedInvoices(new Map());
    setRejectedInvoices(new Set());
    setTotalQueued(0);
    setPreReviewEntries([]);
    setStage("books");
    localStorage.removeItem(STORAGE_KEY_INVOICES);
    localStorage.removeItem(STORAGE_KEY_STAGE);
    setBooksNonce((n) => n + 1);
    setResetting(false);
  }, []);

  /** Llamado por BooksManager tras un upload exitoso con detected_summary>0.
   *  Acumula los archivos por libro y transiciona a pre-review. */
  const handleUploadComplete = useCallback(
    (bookId: Libro, files: BookFile[]) => {
      if (files.length === 0) return;
      setPreReviewEntries((prev) => {
        const existingIdx = prev.findIndex((e) => e.bookId === bookId);
        if (existingIdx === -1) return [...prev, { bookId, files }];
        // Merge: reemplazar archivos por nombre, añadir los nuevos.
        const merged = [...prev[existingIdx].files];
        for (const f of files) {
          const idx = merged.findIndex((m) => m.name === f.name);
          if (idx >= 0) merged[idx] = f;
          else merged.push(f);
        }
        const copy = [...prev];
        copy[existingIdx] = { bookId, files: merged };
        return copy;
      });
      setStage("pre-review");
    },
    [],
  );

  const handlePreReviewFileUpdated = useCallback(
    (bookId: Libro, fileName: string, patch: Partial<BookFile>) => {
      setPreReviewEntries((prev) =>
        prev.map((entry) =>
          entry.bookId !== bookId
            ? entry
            : {
                ...entry,
                files: entry.files.map((f) =>
                  f.name === fileName ? { ...f, ...patch } : f,
                ),
              },
        ),
      );
    },
    [],
  );

  const handlePreReviewFileRemoved = useCallback(
    (bookId: Libro, fileName: string) => {
      setPreReviewEntries((prev) =>
        prev
          .map((entry) =>
            entry.bookId !== bookId
              ? entry
              : { ...entry, files: entry.files.filter((f) => f.name !== fileName) },
          )
          .filter((entry) => entry.files.length > 0),
      );
    },
    [],
  );

  const handleApprove = useCallback(
    (
      id: string,
      data: { formData: Record<string, string>; fiscalLines: FiscalLine[]; libro?: Libro }
    ) => {
      setApprovedInvoices((prev) => {
        const next = new Map(prev);
        const existing = next.get(id);
        next.set(id, {
          formData: data.formData,
          fiscalLines: data.fiscalLines,
          libro: data.libro ?? existing?.libro,
          cuenta_contable: existing?.cuenta_contable || "",
        });
        return next;
      });
      setRejectedInvoices((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    },
    []
  );

  const handleReject = useCallback((id: string) => {
    setRejectedInvoices((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    setApprovedInvoices((prev) => {
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
  }, []);

  if (!hydrated) return null;

  return (
    <div className="w-full h-screen bg-slate-50 overflow-hidden flex flex-col">
      <UpdateBanner />
      <div className="flex-shrink-0 px-6 py-2 bg-white border-b border-slate-100">
        <StageIndicator
          stage={stage}
          onStageClick={(s) => setStage(s)}
          onReset={handleReset}
          resetting={resetting}
          onOpenHistorial={() => setHistorialOpen(true)}
        />
      </div>

      <div className="flex-1 overflow-hidden">
        {stage === "books" && (
          <BooksManager
            key={booksNonce}
            onPipelineStart={() => setStage("processing")}
            onUploadComplete={handleUploadComplete}
            parentPreReviewFilesByBook={Object.fromEntries(
              preReviewEntries.map((e) => [e.bookId, e.files] as const),
            )}
            onFileRemoved={handlePreReviewFileRemoved}
            onNavigateToReview={(focusDocIds) => {
              setReviewFocusIds(focusDocIds);
              setStage("review");
            }}
          />
        )}

        {stage === "pre-review" && (
          <PreReviewStage
            entries={preReviewEntries}
            onFileUpdated={handlePreReviewFileUpdated}
            onFileRemoved={handlePreReviewFileRemoved}
            onBack={() => setStage("books")}
            onPipelineStart={() => {
              setPreReviewEntries([]);
              setStage("processing");
            }}
          />
        )}

        {stage === "processing" && (
          <PipelineProgress
            onComplete={() => setStage("review")}
            onBack={() => setStage("books")}
            onJumpToReview={(total) => { setTotalQueued(total); setStage("review"); }}
          />
        )}

        {stage === "review" && (
          <InvoiceReviewer
            approvedInvoices={approvedInvoices}
            rejectedInvoices={rejectedInvoices}
            totalQueued={totalQueued > 0 ? totalQueued : undefined}
            focusDocIds={reviewFocusIds}
            onClearFocus={() => setReviewFocusIds(undefined)}
            onApprove={handleApprove}
            onReject={handleReject}
            onExport={() => setStage("export")}
          />
        )}

        {stage === "export" && (
          <ExportStage
            approvedInvoices={approvedInvoices}
            onBack={() => setStage("review")}
            onConfirmed={() => {
              // Tras confirmar el lote: limpiar estado in-memory + localStorage
              // y volver a Gestión. Los asientos quedan persistidos en backend
              // (resultado_final.json + maestro_clientes.yaml + .state.json done)
              // y se consultan desde el botón HISTORIAL.
              setApprovedInvoices(new Map());
              setRejectedInvoices(new Set());
              localStorage.removeItem(STORAGE_KEY_INVOICES);
              localStorage.removeItem(STORAGE_KEY_STAGE);
              setStage("books");
            }}
          />
        )}
      </div>

      <FeedbackButton />
      <Toaster position="bottom-right" closeButton richColors />

      <HistorialOverlay
        open={historialOpen}
        onClose={() => setHistorialOpen(false)}
      />
    </div>
  );
}
