"use client";

import { useState, useCallback, useEffect } from "react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { StageIndicator } from "@/components/stage-indicator";
import { BooksManager } from "@/components/books-manager";
import { PipelineProgress } from "@/components/pipeline-progress";
import { InvoiceReviewer } from "@/components/invoice-reviewer";
import { ExportStage } from "@/components/export-stage";
import { FeedbackButton } from "@/components/feedback-button";
import { UpdateBanner } from "@/components/update-banner";
import { ApprovedInvoiceData, FiscalLine, Libro } from "@/lib/types";
import { resetPipeline, fetchInvoices } from "@/lib/api";

type Stage = "books" | "processing" | "review" | "export";

const STORAGE_KEY_INVOICES = "horeca_approved_invoices";
const STORAGE_KEY_STAGE = "horeca_current_stage";

export default function Home() {
  const [hydrated, setHydrated] = useState(false);
  const [stage, setStage] = useState<Stage>("books");
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
  const handleReset = useCallback(async () => {
    setResetting(true);
    try {
      await resetPipeline();
      toast.success("Sesión reseteada. Archivos devueltos a sus carpetas originales.");
    } catch (err) {
      console.error("Reset backend failed:", err);
      toast.warning("No se pudo contactar el backend. Se limpia solo el estado local.");
    }
    setApprovedInvoices(new Map());
    setRejectedInvoices(new Set());
    setStage("books");
    localStorage.removeItem(STORAGE_KEY_INVOICES);
    localStorage.removeItem(STORAGE_KEY_STAGE);
    setResetting(false);
  }, []);

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
        />
      </div>

      <div className="flex-1 overflow-hidden">
        {stage === "books" && (
          <BooksManager onPipelineStart={() => setStage("processing")} />
        )}

        {stage === "processing" && (
          <PipelineProgress
            onComplete={() => setStage("review")}
            onBack={() => setStage("books")}
            onJumpToReview={() => setStage("review")}
          />
        )}

        {stage === "review" && (
          <InvoiceReviewer
            approvedInvoices={approvedInvoices}
            rejectedInvoices={rejectedInvoices}
            onApprove={handleApprove}
            onReject={handleReject}
            onExport={() => setStage("export")}
          />
        )}

        {stage === "export" && (
          <ExportStage
            approvedInvoices={approvedInvoices}
            onBack={() => setStage("review")}
          />
        )}
      </div>

      <FeedbackButton />
      <Toaster position="bottom-right" closeButton richColors />
    </div>
  );
}
