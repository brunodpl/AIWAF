"use client";

import { useState, useEffect } from "react";
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
import { Check, X, ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
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

export function InvoiceReviewer() {
  const [invoiceSummaries, setInvoiceSummaries] = useState<InvoiceSummary[]>([]);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Cache de detalles: se cargan on-demand al navegar
  const [detailsCache, setDetailsCache] = useState<Map<string, InvoiceDocument>>(new Map());
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Cargar aprobaciones previas desde localStorage
  const [approvedInvoices, setApprovedInvoices] = useState<Set<string>>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("approved_invoices");
      if (saved) {
        try {
          const parsed = JSON.parse(saved);
          return new Set(parsed);
        } catch {
          return new Set();
        }
      }
    }
    return new Set();
  });

  // Form data states — must be before conditional returns
  const [formData, setFormData] = useState<Record<string, string>>({});
  const [fiscalLines, setFiscalLines] = useState<FiscalLine[]>([]);

  // Load invoice list (solo resumen, sin detalles)
  useEffect(() => {
    const loadInvoices = async () => {
      try {
        setLoading(true);
        setError(null);

        const response = await fetchInvoices();

        if (response.invoices.length === 0) {
          setError("No hay facturas procesadas. Ejecuta el pipeline primero.");
          setInvoiceSummaries([]);
          return;
        }

        setInvoiceSummaries(response.invoices);
      } catch (err) {
        console.error("Error loading invoices:", err);
        setError("Error cargando facturas. Verifica que el pipeline está funcionando.");
        toast.error("Error cargando facturas");
      } finally {
        setLoading(false);
      }
    };

    loadInvoices();
  }, []);

  // Cargar detalle on-demand cuando cambia el índice
  useEffect(() => {
    const summary = invoiceSummaries[currentIdx];
    if (!summary) return;
    if (detailsCache.has(summary.id)) {
      const cached = detailsCache.get(summary.id)!;
      const nextData: Record<string, string> = {};
      cached.fields.forEach(f => { nextData[f.id] = f.value; });
      setFormData(nextData);
      setFiscalLines(cached.fiscalLines);
      return;
    }

    let cancelled = false;
    const loadDetail = async () => {
      setLoadingDetail(true);
      try {
        const detail = await fetchInvoiceDetail(summary.id);
        const invoice = transformToInvoice(detail);
        if (!cancelled) {
          setDetailsCache(prev => new Map(prev).set(summary.id, invoice));
          const nextData: Record<string, string> = {};
          invoice.fields.forEach(f => { nextData[f.id] = f.value; });
          setFormData(nextData);
          setFiscalLines(invoice.fiscalLines);
        }
      } catch (err) {
        console.error(`Error loading detail for ${summary.id}:`, err);
        if (!cancelled) {
          toast.error(`Error cargando factura ${summary.id}`);
        }
      } finally {
        if (!cancelled) setLoadingDetail(false);
      }
    };

    loadDetail();
    return () => { cancelled = true; };
  }, [currentIdx, invoiceSummaries, detailsCache]);

  // Persist approved invoices to localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      localStorage.setItem(
        "approved_invoices",
        JSON.stringify(Array.from(approvedInvoices))
      );
    }
  }, [approvedInvoices]);

  const invoice = detailsCache.get(invoiceSummaries[currentIdx]?.id);
  
  // Construct file URL from summary (available immediately, no need to wait for detail)
  const currentSummary = invoiceSummaries[currentIdx];
  const fileUrl = currentSummary ? `${API_URL}/api/invoices/${currentSummary.id}/file` : "";

  // Show loading state
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

  // Show error state
  if (error || invoiceSummaries.length === 0) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">{error || "No hay facturas disponibles"}</p>
          <Button
            onClick={() => window.location.reload()}
            variant="outline"
            className="text-xs"
          >
            Reintentar
          </Button>
        </div>
      </div>
    );
  }

  const isApproved = invoice ? approvedInvoices.has(invoice.id) : false;

  const handleInputChange = (id: string, value: string) => {
    setFormData(prev => ({ ...prev, [id]: value }));
  };

  const handleLineChange = (id: string, field: keyof FiscalLine, value: string) => {
    setFiscalLines(prev => prev.map(line =>
      line.id === id ? { ...line, [field]: value === "" ? 0 : (field === "id" ? value : parseFloat(value) || 0) } : line
    ));
  };

  const handleApprove = async () => {
    if (!invoice) return;
    try {
      await sendInvoiceAction(invoice.id, "approve");

      const newApproved = new Set(approvedInvoices);
      newApproved.add(invoice.id);
      setApprovedInvoices(newApproved);
      toast.success("Factura aprobada y guardada");

      if (currentIdx < invoiceSummaries.length - 1) {
         setTimeout(() => goToNext(), 500);
      }
    } catch (err) {
      console.error("Error approving invoice:", err);
      toast.error("Error al aprobar la factura");
    }
  };

  const handleReject = async () => {
    if (!invoice) return;
    try {
      await sendInvoiceAction(invoice.id, "reject");
      toast.error("Factura rechazada");
      if (currentIdx < invoiceSummaries.length - 1) {
        setTimeout(() => goToNext(), 500);
      }
    } catch (err) {
      console.error("Error rejecting invoice:", err);
      toast.error("Error al rechazar la factura");
    }
  };

  const goToPrev = () => {
    if (currentIdx > 0) setCurrentIdx(currentIdx - 1);
  };

  const goToNext = () => {
    if (currentIdx < invoiceSummaries.length - 1) setCurrentIdx(currentIdx + 1);
  };

  return (
    <div 
      className="flex flex-col h-screen bg-white text-slate-900 overflow-hidden text-sm group"
      data-approved={isApproved}
    >
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
          <span className="text-[10px] font-bold text-slate-400 font-mono tracking-tighter uppercase underline decoration-slate-200 underline-offset-4">Extract Lote: 2026-Q2</span>
        </div>

        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1 bg-white border p-1 rounded-sm shadow-sm scale-95">
            <Button 
               variant="ghost" 
               size="icon" 
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToPrev}
               disabled={currentIdx === 0}
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
            >
               <ChevronRight className="h-4 w-4 text-slate-400" />
            </Button>
          </div>
        </div>

        <div className="flex items-center gap-3">
           <span className="text-[10px] font-black text-slate-300 uppercase tracking-widest font-mono">
             #{invoiceSummaries[currentIdx]?.id}
             {loadingDetail && (
               <Loader2 className="h-3 w-3 animate-spin inline ml-1 text-slate-400" />
             )}
           </span>
        </div>
      </header>

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
                        <Label htmlFor={field.id} className="text-[10px] font-black text-slate-400 uppercase tracking-[0.15em] pl-1">
                          {field.label}
                        </Label>
                        <Input
                          id={field.id}
                          value={formData[field.id] || ""}
                          onChange={(e) => handleInputChange(field.id, e.target.value)}
                          className="border-slate-100 focus-visible:ring-0 focus-visible:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all focus-visible:shadow-sm"
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
                                 {fiscalLines.map((line) => (
                                    <tr key={line.id} className="hover:bg-slate-50/30 transition-colors">
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
                                          <div className="flex items-center justify-center">
                                             <Input 
                                               value={line.vatRate} 
                                               onChange={(e) => handleLineChange(line.id, 'vatRate', e.target.value)}
                                               className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs text-center bg-transparent w-16"
                                             />
                                             <span className="text-slate-200">%</span>
                                          </div>
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
               src={invoice?.imageUrl || fileUrl}
               isLoading={!invoice}
               fileType={invoice?.fileType || "image"}
             />
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>

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
          disabled={!invoice || loadingDetail}
          className="w-52 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all disabled:opacity-50"
        >
          Confirmar y Aprobar
        </Button>
      </footer>
    </div>
  );
}
