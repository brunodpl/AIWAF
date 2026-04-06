"use client";

import { useState, useCallback, useEffect } from "react";
import { Toaster } from "@/components/ui/sonner";
import { StageIndicator } from "@/components/stage-indicator";
import { BooksManager } from "@/components/books-manager";
import { PipelineProgress } from "@/components/pipeline-progress";
import { InvoiceReviewer } from "@/components/invoice-reviewer";
import { ExportStage } from "@/components/export-stage";
import { ApprovedInvoiceData, FiscalLine } from "@/lib/types";

type Stage = "books" | "processing" | "review" | "export";

const STORAGE_KEY_INVOICES = "horeca_approved_invoices";
const STORAGE_KEY_STAGE = "horeca_current_stage";

export default function Home() {
  // Restore stage from localStorage (only if there are approved invoices to review)
  const [stage, setStage] = useState<Stage>(() => {
    if (typeof window !== "undefined") {
      const savedInvoices = localStorage.getItem(STORAGE_KEY_INVOICES);
      if (savedInvoices) {
        try {
          const entries = JSON.parse(savedInvoices) as [string, ApprovedInvoiceData][];
          if (entries.length > 0) {
            const savedStage = localStorage.getItem(STORAGE_KEY_STAGE);
            if (savedStage && ["review", "export"].includes(savedStage)) {
              return savedStage as Stage;
            }
            return "review";
          }
        } catch { /* ignore corrupt data */ }
      }
    }
    return "books";
  });

  // Restore approved invoices from localStorage
  const [approvedInvoices, setApprovedInvoices] = useState<
    Map<string, ApprovedInvoiceData>
  >(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem(STORAGE_KEY_INVOICES);
      if (saved) {
        try {
          const entries = JSON.parse(saved) as [string, ApprovedInvoiceData][];
          return new Map(entries);
        } catch { /* ignore corrupt data */ }
      }
    }
    return new Map();
  });

  // Persist approvedInvoices to localStorage with debounce
  useEffect(() => {
    const timer = setTimeout(() => {
      if (typeof window !== "undefined") {
        localStorage.setItem(
          STORAGE_KEY_INVOICES,
          JSON.stringify(Array.from(approvedInvoices.entries()))
        );
      }
    }, 500);

    return () => clearTimeout(timer);
  }, [approvedInvoices]);

  // Persist stage to localStorage on every change
  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY_STAGE, stage);
    }
  }, [stage]);

  const handleApprove = useCallback(
    (id: string, data: { formData: Record<string, string>; fiscalLines: FiscalLine[] }) => {
      setApprovedInvoices((prev) => {
        const next = new Map(prev);
        const existing = next.get(id);
        next.set(id, {
          formData: data.formData,
          fiscalLines: data.fiscalLines,
          clase_fiscal: existing?.clase_fiscal || "gasto_deducible_interior",
          cuenta_contable: existing?.cuenta_contable || "",
        });
        return next;
      });
    },
    []
  );

  const handleUpdateInvoiceData = useCallback(
    (id: string, updates: Partial<Pick<ApprovedInvoiceData, "clase_fiscal" | "cuenta_contable">>) => {
      setApprovedInvoices((prev) => {
        const next = new Map(prev);
        const existing = next.get(id);
        if (existing) {
          next.set(id, { ...existing, ...updates });
        }
        return next;
      });
    },
    []
  );

  const approvedIds = new Set(approvedInvoices.keys());

  return (
    <div className="w-full h-screen bg-slate-50 overflow-hidden flex flex-col">
      {/* Stage indicator bar — positioned at top */}
      <div className="flex-shrink-0 px-6 py-2 bg-white border-b border-slate-100">
        <StageIndicator stage={stage} />
      </div>

      {/* Stage content */}
      <div className="flex-1 overflow-hidden">
        {stage === "books" && (
          <BooksManager onPipelineStart={() => setStage("processing")} />
        )}

        {stage === "processing" && (
          <PipelineProgress
            onComplete={() => setStage("review")}
            onBack={() => setStage("books")}
          />
        )}

        {stage === "review" && (
          <InvoiceReviewer
            approvedInvoices={approvedIds}
            onApprove={handleApprove}
            onExport={() => setStage("export")}
          />
        )}

        {stage === "export" && (
          <ExportStage
            approvedInvoices={approvedInvoices}
            onUpdateInvoice={handleUpdateInvoiceData}
            onBack={() => setStage("review")}
          />
        )}
      </div>

      <Toaster position="top-right" closeButton richColors />
    </div>
  );
}
