"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ImageViewer } from "./image-viewer";
import { toast } from "sonner";
import { cn } from "../lib/utils";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Check, ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { FiscalLine, InvoiceDocument } from "@/lib/types";
import {
  fetchInvoices,
  fetchInvoiceDetail,
  sendInvoiceAction,
  transformToInvoice,
  API_URL,
} from "@/lib/api";

interface InvoiceSummary {
  id: string;
  decision_global: string;
  timestamp: string;
  nif_entidad: string;
  nombre_entidad: string;
  numero_factura: string;
  total_euros: number;
}

interface InvoiceReviewerProps {
  approvedInvoices: Set<string>;
  onApprove: (id: string, data: { formData: Record<string, string>; fiscalLines: FiscalLine[] }) => void;
  onExport: () => void;
}

export function InvoiceReviewer({ approvedInvoices, onApprove, onExport }: InvoiceReviewerProps) {
  const [invoiceSummaries, setInvoiceSummaries] = useState<InvoiceSummary[]>([]);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Details cache — stored in state but accessed via ref inside effects to avoid re-triggering
  const [detailsCache, setDetailsCache] = useState<Map<string, InvoiceDocument>>(new Map());
  const cacheRef = useRef<Map<string, InvoiceDocument>>(new Map());

  // Refs for latest values (avoids stale closures in setTimeout/callbacks)
  const currentIdxRef = useRef(0);
  const invoiceSummariesRef = useRef<InvoiceSummary[]>([]);

  // Keep refs in sync
  useEffect(() => {
    currentIdxRef.current = currentIdx;
  }, [currentIdx]);

  useEffect(() => {
    invoiceSummariesRef.current = invoiceSummaries;
  }, [invoiceSummaries]);

  // Keep cacheRef in sync with state
  useEffect(() => {
    cacheRef.current = detailsCache;
  }, [detailsCache]);

  // Form data states
  const [formData, setFormData] = useState<Record<string, string>>({});
  const [fiscalLines, setFiscalLines] = useState<FiscalLine[]>([]);

  // File type cache to avoid guessing
  const [fileType, setFileType] = useState<"pdf" | "image">("image");
  const fileTypeCache = useRef<Map<string, "pdf" | "image">>(new Map());

  // Loading detail state (must be before the detail useEffect)
  const [loadingDetail, setLoadingDetailState] = useState(false);
  const loadingDetailRef = useRef(false);
  const setLoadingDetail = (v: boolean) => {
    loadingDetailRef.current = v;
    setLoadingDetailState(v);
  };

  // Current file URL (computed from summary, not from detail)
  const [fileUrl, setFileUrl] = useState("");

  // ── Load invoice list ──────────────────────────────────────────────────
  const loadInvoices = useCallback(async (isRefresh = false) => {
    try {
      if (isRefresh) setRefreshing(true);
      else { setLoading(true); setError(null); }

      const response = await fetchInvoices();

      if (response.invoices.length === 0) {
        setError("No hay facturas procesadas. Ejecuta el pipeline primero.");
        setInvoiceSummaries([]);
        return;
      }

      setInvoiceSummaries(response.invoices);

      // Reset index if out of bounds
      if (currentIdxRef.current >= response.invoices.length) {
        setCurrentIdx(0);
      }
    } catch (err) {
      console.error("Error loading invoices:", err);
      if (!isRefresh) {
        setError("Error cargando facturas. Verifica que el pipeline está funcionando.");
      }
      toast.error("Error cargando facturas");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadInvoices();
  }, [loadInvoices]);

  // ── Load detail on-demand when index changes ───────────────────────────
  useEffect(() => {
    const summary = invoiceSummaries[currentIdx];
    if (!summary) return;

    // Check cache via ref (avoid stale state)
    const cached = cacheRef.current.get(summary.id);
    if (cached) {
      const nextData: Record<string, string> = {};
      cached.fields.forEach(f => { nextData[f.id] = f.value; });
      setFormData(nextData);
      setFiscalLines(cached.fiscalLines);
      setFileUrl(cached.imageUrl);
      setFileType(cached.fileType);
      return;
    }

    // Not cached — fetch with AbortController
    const controller = new AbortController();
    let cancelled = false;

    const loadDetail = async () => {
      setLoadingDetail(true);
      try {
        const detail = await fetchInvoiceDetail(summary.id);
        if (cancelled) return;

        const invoice = transformToInvoice(detail);

        // Also resolve file type from the detail
        const resolvedFileType = invoice.fileType;

        // Update cache
        setDetailsCache(prev => {
          const next = new Map(prev);
          next.set(summary.id, invoice);
          return next;
        });

        // Update form state
        const nextData: Record<string, string> = {};
        invoice.fields.forEach(f => { nextData[f.id] = f.value; });
        setFormData(nextData);
        setFiscalLines(invoice.fiscalLines);
        setFileUrl(invoice.imageUrl);
        setFileType(resolvedFileType);
      } catch (err) {
        if (cancelled) return;
        console.error(`Error loading detail for ${summary.id}:`, err);
        toast.error(`Error cargando factura ${summary.id}`);
        setFormData({});
        setFiscalLines([]);
      } finally {
        if (!cancelled) setLoadingDetail(false);
      }
    };

    loadDetail();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [currentIdx, invoiceSummaries]);

  // ── Determine file URL for display (before detail loads) ───────────────
  const currentSummary = invoiceSummaries[currentIdx];
  const displayFileUrl = currentSummary
    ? `${API_URL}/api/invoices/${currentSummary.id}/file`
    : "";

  // The file URL to show in ImageViewer:
  // Use cached fileUrl if we have the detail, otherwise use the direct endpoint
  const activeFileUrl = fileUrl || displayFileUrl;

  // ── Handlers (MUST be before conditional returns — Rules of Hooks) ─────
  const handleInputChange = (id: string, value: string) => {
    setFormData(prev => ({ ...prev, [id]: value }));
  };

  const handleLineChange = (id: string, field: keyof FiscalLine, value: string) => {
    setFiscalLines(prev => prev.map(line =>
      line.id === id
        ? { ...line, [field]: value === "" ? 0 : (field === "id" ? value : parseFloat(value) || 0) }
        : line
    ));
  };

  const goToPrev = useCallback(() => {
    if (currentIdxRef.current > 0) {
      setCurrentIdx(currentIdxRef.current - 1);
    }
  }, []);

  const goToNext = useCallback(() => {
    const summaries = invoiceSummariesRef.current;
    if (currentIdxRef.current < summaries.length - 1) {
      setCurrentIdx(currentIdxRef.current + 1);
    }
  }, []);

  const handleApprove = async () => {
    const currentInvoice = detailsCache.get(invoiceSummariesRef.current[currentIdxRef.current]?.id);
    if (!currentInvoice) return;

    const confirmed = window.confirm(
      `¿Confirmar aprobación de la factura ${currentInvoice.id}?\n\nSe registrará esta acción en el log de auditoría.`
    );
    if (!confirmed) return;

    try {
      await sendInvoiceAction(currentInvoice.id, "approve", {
        fields: formData,
        fiscalLines: fiscalLines.map(l => ({
          base: Number(l.base),
          tipo_iva: l.vatRate !== null ? Number(l.vatRate) : null,
          cuota: Number(l.vatAmount),
          total: Number(l.total),
        })),
      });

      onApprove(currentInvoice.id, { formData, fiscalLines });
      toast.success("Factura aprobada correctamente");

      // Auto-advance using ref-based navigation (no stale closure)
      if (currentIdxRef.current < invoiceSummariesRef.current.length - 1) {
        setTimeout(() => {
          const summaries = invoiceSummariesRef.current;
          const idx = currentIdxRef.current;
          if (idx < summaries.length - 1) {
            setCurrentIdx(idx + 1);
          }
        }, 500);
      }
    } catch (err) {
      console.error("Error approving invoice:", err);
      toast.error("Error al aprobar la factura");
    }
  };

  const handleReject = async () => {
    const currentInvoice = detailsCache.get(invoiceSummariesRef.current[currentIdxRef.current]?.id);
    if (!currentInvoice) return;

    const confirmed = window.confirm(
      `¿Confirmar rechazo de la factura ${currentInvoice.id}?\n\nSe moverá a INCIDENCIAS para revisión.`
    );
    if (!confirmed) return;

    try {
      await sendInvoiceAction(currentInvoice.id, "reject", {
        fields: formData,
        fiscalLines: fiscalLines.map(l => ({
          base: Number(l.base),
          tipo_iva: l.vatRate !== null ? Number(l.vatRate) : null,
          cuota: Number(l.vatAmount),
          total: Number(l.total),
        })),
      });
      toast.error("Factura rechazada");

      if (currentIdxRef.current < invoiceSummariesRef.current.length - 1) {
        setTimeout(() => {
          const summaries = invoiceSummariesRef.current;
          const idx = currentIdxRef.current;
          if (idx < summaries.length - 1) {
            setCurrentIdx(idx + 1);
          }
        }, 500);
      }
    } catch (err) {
      console.error("Error rejecting invoice:", err);
      toast.error("Error al rechazar la factura");
    }
  };

  const handleRefresh = () => {
    loadInvoices(true);
  };

  // ── Loading / Error states (AFTER all hooks) ──────────────────────────
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
          <p className="text-sm text-slate-500">Cargando facturas...</p>
        </div>
      </div>
    );
  }

  if (error || invoiceSummaries.length === 0) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">{error || "No hay facturas disponibles"}</p>
          <Button onClick={() => loadInvoices()} variant="outline" className="text-xs">
            Reintentar
          </Button>
        </div>
      </div>
    );
  }

  // Computed values (after early returns — not hooks, safe here)
  const invoice = detailsCache.get(invoiceSummaries[currentIdx]?.id);
  const isApproved = invoice ? approvedInvoices.has(invoice.id) : false;
  const totalInvoices = invoiceSummaries.length;
  const approvedCount = invoiceSummaries.filter(inv => approvedInvoices.has(inv.id)).length;

  return (
    <div
      className="flex flex-col h-screen bg-white text-slate-900 overflow-hidden text-sm group"
      data-approved={isApproved}
    >
      {/* Header */}
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0 z-10 transition-colors group-data-[approved=true]:bg-green-50/20">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">Revisión de Factura</h2>
            {isApproved && (
               <div className="bg-green-600 rounded-full p-0.5 animate-in zoom-in duration-300">
                  <Check className="h-2.5 w-2.5 text-white stroke-[4]" />
               </div>
            )}
          </div>
          <div className="h-4 w-[1px] bg-slate-200" />
          <span className="text-[10px] font-bold text-slate-400 font-mono">
            {currentIdx + 1} / {totalInvoices}
            {approvedCount > 0 && (
              <span className="ml-2 text-emerald-500">({approvedCount} aprobadas)</span>
            )}
          </span>
        </div>

        {/* Navigation */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1 bg-white border p-1 rounded-sm shadow-sm">
            <Button
               variant="ghost"
               size="icon"
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToPrev}
               disabled={currentIdx === 0}
               aria-label="Factura anterior"
            >
               <ChevronLeft className="h-4 w-4 text-slate-400" />
            </Button>

            <div className="flex items-center gap-1.5 px-3">
               {invoiceSummaries.map((inv, idx) => {
                  const done = approvedInvoices.has(inv.id);
                  const active = currentIdx === idx;
                  return (
                    <button
                      key={inv.id}
                      onClick={() => setCurrentIdx(idx)}
                      className={cn(
                        "w-5 h-5 text-[9px] font-black rounded-full flex items-center justify-center transition-all",
                        active ? "bg-slate-900 text-white scale-110 shadow-md" : "hover:bg-slate-100 text-slate-300",
                        done && !active && "text-green-500 bg-green-50/50",
                        done && active && "bg-green-600"
                      )}
                      aria-label={`Ir a factura ${inv.id}`}
                    >
                      {done && !active ? <Check className="h-2.5 w-2.5 stroke-[4]" /> : idx + 1}
                    </button>
                  );
               })}
            </div>

            <Button
               variant="ghost"
               size="icon"
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToNext}
               disabled={currentIdx === invoiceSummaries.length - 1}
               aria-label="Factura siguiente"
            >
               <ChevronRight className="h-4 w-4 text-slate-400" />
            </Button>
          </div>

          {/* Refresh button */}
          <Button
            variant="ghost"
            size="sm"
            onClick={handleRefresh}
            disabled={refreshing}
            className="text-xs rounded-none"
            aria-label="Actualizar lista de facturas"
          >
            <RefreshCw className={cn("h-3 w-3 mr-1", refreshing && "animate-spin")} />
            Actualizar
          </Button>
        </div>

        {/* Right side */}
        <div className="flex items-center gap-3">
           <span className="text-[10px] font-black text-slate-300 uppercase tracking-widest font-mono">
             #{invoiceSummaries[currentIdx]?.id}
             {loadingDetail && (
               <Loader2 className="h-3 w-3 animate-spin inline ml-1 text-slate-400" />
             )}
           </span>
           {invoice && (
             <span className={cn(
               "text-[10px] font-bold px-2 py-1 rounded-full",
               invoice.decision_global === "auto" && "bg-emerald-100 text-emerald-700",
               invoice.decision_global === "warn" && "bg-amber-100 text-amber-700",
               invoice.decision_global === "block" && "bg-red-100 text-red-700",
               invoice.decision_global === "pendiente" && "bg-slate-100 text-slate-600",
             )}>
               {invoice.decision_global?.toUpperCase()}
             </span>
           )}
           {approvedInvoices.size > 0 && (
             <Button
               variant="outline"
               size="sm"
               onClick={onExport}
               className="text-[10px] uppercase tracking-[0.15em] rounded-none border-teal-200 text-teal-700 hover:bg-teal-50"
             >
               Generar Asientos →
             </Button>
           )}
        </div>
      </header>

      {/* Main content */}
      <div className="flex flex-grow overflow-hidden border-b transition-opacity group-data-[approved=true]:opacity-90">
        <ResizablePanelGroup direction="horizontal">
          <ResizablePanel defaultSize={45} minSize={30}>
            <div className="h-full flex flex-col">
              <div className="p-4 border-b bg-slate-50 flex items-center justify-between flex-shrink-0">
                <h1 className="text-xs font-black uppercase tracking-widest text-slate-400">Datos Factura</h1>
              </div>
              <ScrollArea className="flex-grow">
                {invoice ? (
                <div className="p-10 space-y-10 max-w-2xl mx-auto">
                  <div className="grid grid-cols-2 gap-x-8 gap-y-6">
                    {invoice.fields.map((field) => (
                      <div key={field.id} className="space-y-2">
                        <div className="flex items-center gap-2">
                          <Label htmlFor={field.id} className="text-[10px] font-black text-slate-400 uppercase tracking-[0.15em] pl-1">
                            {field.label}
                          </Label>
                          <span className={cn(
                            "text-[9px] font-bold px-1.5 py-0.5 rounded",
                            field.status === "auto" && "bg-emerald-50 text-emerald-600",
                            field.status === "warn" && "bg-amber-50 text-amber-600",
                            field.status === "block" && "bg-red-50 text-red-600",
                          )}>
                            {field.status.toUpperCase()} {field.confidence}%
                          </span>
                        </div>
                        <Input
                          id={field.id}
                          value={formData[field.id] || ""}
                          onChange={(e) => handleInputChange(field.id, e.target.value)}
                          className={cn(
                            "border-slate-100 focus-visible:ring-0 focus-visible:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all focus-visible:shadow-sm",
                            field.status === "block" && "border-red-200 bg-red-50/30",
                            field.status === "warn" && "border-amber-200",
                          )}
                        />
                      </div>
                    ))}
                  </div>

                  {fiscalLines.length > 0 && (
                     <div className="pt-10 border-t">
                        <Label className="text-[11px] font-black uppercase text-slate-900 block mb-6 tracking-[0.1em] pl-1">Desglose de Líneas</Label>
                        <div className="border border-slate-100 overflow-hidden shadow-[0_2px_10px_-4px_rgba(0,0,0,0.05)]">
                           <table className="w-full text-xs font-mono border-collapse">
                              <thead>
                                 <tr className="bg-slate-50/50 border-b border-slate-100 text-[10px] text-slate-400 uppercase font-black">
                                    <th className="p-3 text-left w-1/4 tracking-widest">Base</th>
                                    <th className="p-3 text-center tracking-widest">IVA %</th>
                                    <th className="p-3 text-right tracking-widest">Cuota</th>
                                    <th className="p-3 text-right tracking-widest">Total</th>
                                 </tr>
                              </thead>
                              <tbody className="divide-y divide-slate-50">
                                 {fiscalLines.map((line, idx) => (
                                    <tr key={`${invoice.id}_line_${idx}`} className="hover:bg-slate-50/30 transition-colors">
                                       <td className="p-1 px-2">
                                          <div className="flex items-center">
                                             <Input
                                               value={line.base}
                                               onChange={(e) => handleLineChange(line.id, 'base', e.target.value)}
                                               className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs bg-transparent"
                                             />
                                             <span className="text-slate-200 pr-2">€</span>
                                          </div>
                                       </td>
                                       <td className="p-1 px-2">
                                          {line.vatRate === null || line.vatRate === 0 ? (
                                            <span className="text-[10px] font-bold text-amber-600 px-2 py-1">EXENTA</span>
                                          ) : (
                                            <div className="flex items-center justify-center">
                                               <Input
                                                 value={line.vatRate}
                                                 onChange={(e) => handleLineChange(line.id, 'vatRate', e.target.value)}
                                                 className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs text-center bg-transparent w-16"
                                               />
                                               <span className="text-slate-200">%</span>
                                            </div>
                                          )}
                                       </td>
                                       <td className="p-1 px-4 text-right text-slate-400 font-medium tracking-tighter">
                                          {line.vatAmount.toFixed(2)}€
                                       </td>
                                       <td className="p-1 px-2">
                                          <div className="flex items-center justify-end">
                                             <Input
                                               value={line.total}
                                               onChange={(e) => handleLineChange(line.id, 'total', e.target.value)}
                                               className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs text-right font-bold bg-transparent"
                                             />
                                             <span className="text-slate-200 pr-2">€</span>
                                          </div>
                                       </td>
                                    </tr>
                                 ))}
                              </tbody>
                           </table>
                        </div>
                     </div>
                  )}
                  <div className="h-10" />
                </div>
                ) : (
                  <div className="flex h-full items-center justify-center">
                    <div className="text-center">
                      <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
                      <p className="text-sm text-slate-500">Cargando detalle...</p>
                    </div>
                  </div>
                )}
              </ScrollArea>
            </div>
          </ResizablePanel>

          <ResizableHandle className="w-1 bg-slate-50 border-x border-slate-100 transition-colors" />

          <ResizablePanel defaultSize={55} minSize={30}>
             <ImageViewer
               src={activeFileUrl}
               isLoading={!invoice}
               fileType={fileType}
             />
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>

      {/* Footer */}
      <footer className="h-24 flex items-center justify-center gap-6 px-8 bg-white flex-shrink-0">
        <Button
          variant="outline"
          size="lg"
          onClick={handleReject}
          disabled={!invoice || loadingDetail}
          className="w-52 h-11 border border-slate-200 hover:bg-slate-50 hover:text-slate-900 font-bold uppercase text-[10px] tracking-[0.2em] rounded-none transition-all shadow-sm"
        >
          Rechazar
        </Button>
        <Button
          size="lg"
          onClick={handleApprove}
          disabled={!invoice || loadingDetail || isApproved}
          className={cn(
            "w-52 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            isApproved && "opacity-50 cursor-not-allowed"
          )}
        >
          {isApproved ? (
            <>
              <Check className="h-4 w-4 mr-2" />
              Aprobada
            </>
          ) : (
            <>
              <Check className="h-4 w-4 mr-2" />
              Confirmar y Aprobar
            </>
          )}
        </Button>
      </footer>
    </div>
  );
}
