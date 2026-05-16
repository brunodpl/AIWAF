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
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Check, ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { ApprovedInvoiceData, DocStatus, FiscalLine, InvoiceDocument, Libro, LibroShort } from "@/lib/types";
import {
  fetchInvoices,
  fetchInvoiceDetail,
  sendInvoiceAction,
  transformToInvoice,
  API_URL,
} from "@/lib/api";
import { resolveClienteGestoria } from "@/lib/cliente-gestoria";
import { CUENTAS, CUENTA_BY_CODE } from "@/lib/cuentas-maestro";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const HEADER_FIELDS = new Set(["nif_cliente", "nombre_cliente", "concepto"]);

interface InvoiceSummary {
  id: string;
  /** Nombre actual de la carpeta (operario-friendly tras rename). */
  folder_name?: string;
  /** Forma corta del libro: compras / ventas / bienes. */
  libro?: LibroShort | null;
  /** Estado actual del documento (último evento del sidecar). */
  status?: DocStatus | null;
  /** folder_name del asiento original si se detectó duplicado fiscal. */
  duplicate_of?: string;
  /** Hash determinista del triplete (NIF emisor, nº factura, fecha). */
  fiscal_hash?: string;
  decision_global: string;
  timestamp: string;
  nif_entidad: string;
  nombre_entidad: string;
  numero_factura: string;
  total_euros: number;
}

interface InvoiceReviewerProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  rejectedInvoices: Set<string>;
  onApprove: (id: string, data: { formData: Record<string, string>; fiscalLines: FiscalLine[]; libro?: Libro }) => void;
  onReject: (id: string) => void;
  onExport: () => void;
  /** Total de facturas en cola en el pipeline (para mostrar dots pendientes al saltar a revisión anticipadamente). */
  totalQueued?: number;
}

export function InvoiceReviewer({ approvedInvoices, rejectedInvoices, onApprove, onReject, onExport, totalQueued }: InvoiceReviewerProps) {
  const [invoiceSummaries, setInvoiceSummaries] = useState<InvoiceSummary[]>([]);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Guard de doble-click: deshabilita botones durante la llamada API.
  // Combinamos state (para re-render del disabled) con ref (chequeo síncrono
  // antes de que React propague el setState a los handlers).
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  // Dirty flag: detecta cambios sin guardar
  const dirtyRef = useRef(false);
  // Navegación pendiente (cuando hay cambios sin guardar)
  const [pendingNavIdx, setPendingNavIdx] = useState<number | null>(null);
  // Auto-advance timeout ref para cancelar en navegación manual
  const autoAdvanceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Último id de factura cargado en el formulario. Protege contra resets
  // espurios del useEffect de carga cuando ``invoiceSummaries`` cambia de
  // referencia (polling cada 3s) pero el id de la factura actual no varió.
  const lastLoadedIdRef = useRef<string | null>(null);

  const [detailsCache, setDetailsCache] = useState<Map<string, InvoiceDocument>>(new Map());
  const cacheRef = useRef<Map<string, InvoiceDocument>>(new Map());
  const approvedInvoicesRef = useRef<Map<string, ApprovedInvoiceData>>(new Map());

  const currentIdxRef = useRef(0);
  const invoiceSummariesRef = useRef<InvoiceSummary[]>([]);

  useEffect(() => { currentIdxRef.current = currentIdx; }, [currentIdx]);
  useEffect(() => { invoiceSummariesRef.current = invoiceSummaries; }, [invoiceSummaries]);
  useEffect(() => { cacheRef.current = detailsCache; }, [detailsCache]);
  useEffect(() => { approvedInvoicesRef.current = approvedInvoices; }, [approvedInvoices]);

  const [formData, setFormData] = useState<Record<string, string>>({});
  const [fiscalLines, setFiscalLines] = useState<FiscalLine[]>([]);
  const [fileType, setFileType] = useState<"pdf" | "image">("image");
  const [loadingDetail, setLoadingDetailState] = useState(false);
  const loadingDetailRef = useRef(false);
  const setLoadingDetail = (v: boolean) => {
    loadingDetailRef.current = v;
    setLoadingDetailState(v);
  };
  const [fileUrl, setFileUrl] = useState("");

  const loadInvoices = useCallback(async (isRefresh = false) => {
    try {
      if (isRefresh) setRefreshing(true);
      else { setLoading(true); setError(null); }

      const response = await fetchInvoices({ includeDone: true });

      if (response.invoices.length === 0) {
        setError("No hay facturas procesadas. Ejecuta el pipeline primero.");
        setInvoiceSummaries([]);
        return;
      }

      // IMPORTANTE: en refresh, preservamos el orden previo. El backend
      // construye la lista con Path.iterdir() (sin orden estable entre llamadas),
      // por lo que un poll podía hacer "saltar" la factura que el operario
      // estaba revisando de posición. Mergeamos por id: actualizamos campos
      // de las existentes en sitio, y añadimos las nuevas al final.
      setInvoiceSummaries(prev => {
        if (!isRefresh || prev.length === 0) return response.invoices;
        const byId = new Map(response.invoices.map(i => [i.id, i]));
        const stillPresent = prev
          .filter(i => byId.has(i.id))
          .map(i => byId.get(i.id) ?? i);
        const prevIds = new Set(prev.map(i => i.id));
        const newOnes = response.invoices.filter(i => !prevIds.has(i.id));
        return [...stillPresent, ...newOnes];
      });
      // El clamp de currentIdx vive ahora en un useEffect separado (más abajo)
      // que reacciona a invoiceSummaries.length post-merge.
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

  useEffect(() => { loadInvoices(); }, [loadInvoices]);

  // Clamp de currentIdx: si tras un merge la lista se redujo (delete externo,
  // reset mid-polling), evitamos quedar fuera de rango. Apuntamos a la última
  // factura disponible — preferimos no saltar a 0 para no desorientar al
  // usuario si solo cayeron las del final.
  useEffect(() => {
    if (invoiceSummaries.length === 0) return;
    if (currentIdx >= invoiceSummaries.length) {
      setCurrentIdx(invoiceSummaries.length - 1);
    }
  }, [invoiceSummaries.length, currentIdx]);

  // Polling para facturas pendientes de escaneo (al saltar a revisión anticipadamente).
  //
  // Condiciones de salida:
  // 1. Hemos cargado al menos totalQueued facturas (pipeline completó).
  // 2. Máximo 30 intentos (~90s) — evita polling infinito si el pipeline
  //    falla o se cancela y nunca alcanza el target.
  // 3. Cleanup al unmount.
  useEffect(() => {
    if (!totalQueued || totalQueued === 0) return;
    let attempts = 0;
    const MAX_ATTEMPTS = 30;
    const interval = setInterval(() => {
      if (invoiceSummariesRef.current.length >= totalQueued) {
        clearInterval(interval);
        return;
      }
      attempts += 1;
      if (attempts >= MAX_ATTEMPTS) {
        clearInterval(interval);
        toast.warning("El pipeline no completó el conteo esperado — algunas facturas pueden faltar");
        return;
      }
      loadInvoices(true);
    }, 3000);
    return () => clearInterval(interval);
  }, [totalQueued, loadInvoices]);

  useEffect(() => {
    const summary = invoiceSummaries[currentIdx];
    if (!summary) return;

    // Salvaguardia anti-clobber: si la factura es la MISMA que ya teníamos
    // cargada y hay edición en curso (``dirtyRef``), no resetear nada — el
    // efecto se ha disparado por un cambio espurio de referencia del array
    // (típicamente el polling de facturas pendientes) que no requiere recarga.
    if (summary.id === lastLoadedIdRef.current && dirtyRef.current) {
      return;
    }
    lastLoadedIdRef.current = summary.id;

    // Limpiar datos stale inmediatamente al cambiar de factura
    setFormData({});
    setFiscalLines([]);
    dirtyRef.current = false;

    // Función auxiliar: aplica datos al formulario, priorizando datos aprobados por el usuario
    // sobre los datos brutos de la API (esto es el "human in the loop" — lo que el humano
    // aprobó es la fuente de verdad, no lo que escaneó la IA).
    const applyInvoiceData = (source: InvoiceDocument) => {
      const approvedData = approvedInvoicesRef.current.get(summary.id);
      if (approvedData) {
        // Factura ya aprobada: restaurar exactamente lo que el usuario aprobó
        setFormData(approvedData.formData);
        setFiscalLines(approvedData.fiscalLines);
      } else {
        // Factura sin aprobar: cargar datos originales de la API
        const nextData: Record<string, string> = {};
        source.fields.forEach(f => { nextData[f.id] = f.value; });
        setFormData(nextData);
        setFiscalLines(source.fiscalLines);
      }
      setFileUrl(source.imageUrl);
      setFileType(source.fileType);
    };

    const cached = cacheRef.current.get(summary.id);
    if (cached) {
      applyInvoiceData(cached);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    const loadDetail = async () => {
      setLoadingDetail(true);
      try {
        const detail = await fetchInvoiceDetail(summary.id);
        if (cancelled) return;

        const invoice = transformToInvoice(detail);

        setDetailsCache(prev => {
          const next = new Map(prev);
          next.set(summary.id, invoice);
          return next;
        });

        applyInvoiceData(invoice);
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
    return () => { cancelled = true; controller.abort(); };
    // Deps:
    // - currentIdx: fires al navegar.
    // - invoiceSummaries[currentIdx]?.id: fires si el summary actual cambia
    //   de id (raro — solo si backend reordena).
    // NO ponemos ``invoiceSummaries`` entero porque el polling cada 3s lo
    // reemplaza con la misma lista y clobberaría el formData mientras el
    // usuario teclea. Bug reportado por operario en fase 3 con pipeline
    // todavía procesando.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentIdx, invoiceSummaries[currentIdx]?.id]);

  const currentSummary = invoiceSummaries[currentIdx];
  const displayFileUrl = currentSummary ? `${API_URL}/api/invoices/${currentSummary.id}/file` : "";
  const activeFileUrl = fileUrl || displayFileUrl;

  const handleInputChange = (id: string, value: string) => {
    dirtyRef.current = true;
    setFormData(prev => ({ ...prev, [id]: value }));
  };

  const handleLineChange = (id: string, field: keyof FiscalLine, value: string) => {
    if (value !== "" && !/^-?\d*(,\d*)?$/.test(value)) return;
    dirtyRef.current = true;
    setFiscalLines(prev => prev.map(line => {
      if (line.id !== id) return line;
      if (field === "id") return { ...line, [field]: value };
      // Preserve intermediate states (empty, minus sign, trailing comma) as raw string
      if (value === "" || value === "-" || value.endsWith(",")) return { ...line, [field]: value as unknown as number };
      const normalized = value.replace(",", ".");
      const parsed = parseFloat(normalized);
      return { ...line, [field]: isNaN(parsed) ? 0 : parsed };
    }));
  };

  // Navegación con check de dirty flag y cancelación de auto-advance
  const cancelAutoAdvance = useCallback(() => {
    if (autoAdvanceRef.current) {
      clearTimeout(autoAdvanceRef.current);
      autoAdvanceRef.current = null;
    }
  }, []);

  const navigateTo = useCallback((idx: number) => {
    cancelAutoAdvance();
    if (dirtyRef.current) {
      setPendingNavIdx(idx);
    } else {
      setCurrentIdx(idx);
    }
  }, [cancelAutoAdvance]);

  const goToPrev = useCallback(() => {
    if (currentIdxRef.current > 0) navigateTo(currentIdxRef.current - 1);
  }, [navigateTo]);

  const goToNext = useCallback(() => {
    const summaries = invoiceSummariesRef.current;
    if (currentIdxRef.current < summaries.length - 1) navigateTo(currentIdxRef.current + 1);
  }, [navigateTo]);

  // Cleanup auto-advance on unmount
  useEffect(() => {
    return () => cancelAutoAdvance();
  }, [cancelAutoAdvance]);

  // Ejecutar la acción (aprobar/rechazar) con guard de doble-click
  const executeConfirmedAction = async (action: "approve" | "reject") => {
    // Guard síncrono: si dos clicks llegan en el mismo tick antes de que
    // React propague setSubmitting(true), el segundo ve submittingRef=true.
    if (submittingRef.current) return;
    const currentInvoice = detailsCache.get(invoiceSummariesRef.current[currentIdxRef.current]?.id);
    if (!currentInvoice) return;

    submittingRef.current = true;
    setSubmitting(true);
    // Capturamos el índice ANTES de la llamada async. El auto-advance solo
    // dispara si seguimos viendo la misma factura al volver — si el usuario
    // navegó manualmente, dejamos su navegación intacta.
    const idxAtSubmit = currentIdxRef.current;
    try {
      await sendInvoiceAction(currentInvoice.id, action, {
        fields: formData,
        fiscalLines: fiscalLines.map(l => {
          const base = parseFloat(String(l.base).replace(',', '.')) || 0;
          const cuota = parseFloat(String(l.vatAmount).replace(',', '.')) || 0;
          const vatRate = l.vatRate !== null ? (parseFloat(String(l.vatRate).replace(',', '.')) || null) : null;
          return { base, tipo_iva: vatRate, cuota, total: base + cuota };
        }),
      });

      dirtyRef.current = false;

      if (action === "approve") {
        onApprove(currentInvoice.id, { formData, fiscalLines, libro: currentInvoice.libro });
        toast.success("Factura aprobada correctamente");
      } else {
        onReject(currentInvoice.id);
        toast.error("Factura rechazada — movida a INCIDENCIAS");
      }

      // Auto-advance solo si el usuario NO ha navegado durante el await.
      if (
        currentIdxRef.current === idxAtSubmit &&
        currentIdxRef.current < invoiceSummariesRef.current.length - 1
      ) {
        autoAdvanceRef.current = setTimeout(() => {
          const summaries = invoiceSummariesRef.current;
          const idx = currentIdxRef.current;
          // Re-chequear: si el usuario navegó dentro de los 500ms, no avanzamos.
          if (idx === idxAtSubmit && idx < summaries.length - 1) {
            setCurrentIdx(idx + 1);
          }
          autoAdvanceRef.current = null;
        }, 500);
      }
    } catch (err) {
      console.error(`Error en acción ${action}:`, err);
      toast.error(`Error al ${action === "approve" ? "aprobar" : "rechazar"} la factura`);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const handleRefresh = () => loadInvoices(true);

  // Loading / Error states
  if (loading) {
    return (
      // FIX #3: h-full en lugar de h-screen (ya estamos dentro de un h-screen en page.tsx)
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
          <p className="text-sm text-slate-500">Cargando facturas...</p>
        </div>
      </div>
    );
  }

  if (error || invoiceSummaries.length === 0) {
    return (
      // FIX #3: h-full en lugar de h-screen
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">{error || "No hay facturas disponibles"}</p>
          <Button onClick={() => loadInvoices()} variant="outline" className="text-xs">
            Reintentar
          </Button>
        </div>
      </div>
    );
  }

  const invoice = detailsCache.get(invoiceSummaries[currentIdx]?.id);
  const isApproved = invoice ? approvedInvoices.has(invoice.id) : false;
  const isRejected = invoice ? rejectedInvoices.has(invoice.id) : false;
  const totalVisible = totalQueued && totalQueued > invoiceSummaries.length ? totalQueued : invoiceSummaries.length;
  const totalInvoices = totalVisible;
  const loadedCount = invoiceSummaries.length;
  const approvedCount = invoiceSummaries.filter(inv => approvedInvoices.has(inv.id)).length;

  return (
    <div
      className="flex flex-col h-full bg-white text-slate-900 overflow-hidden text-sm group"
      data-approved={isApproved}
      data-rejected={isRejected}
    >
      {/* AlertDialog para cambios sin guardar */}
      <AlertDialog open={pendingNavIdx !== null} onOpenChange={(open) => { if (!open) setPendingNavIdx(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cambios sin guardar</AlertDialogTitle>
            <AlertDialogDescription>
              Has editado campos de esta factura sin aprobarla. Si navegas, perderás los cambios.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setPendingNavIdx(null)}>Seguir editando</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                dirtyRef.current = false;
                setCurrentIdx(pendingNavIdx!);
                setPendingNavIdx(null);
              }}
            >
              Descartar cambios
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Header */}
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0 z-10 transition-colors group-data-[approved=true]:bg-green-50/20 group-data-[rejected=true]:bg-red-50/30">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">Revisión de Factura</h2>
            {isApproved && (
               <div className="bg-green-600 rounded-full p-0.5 animate-in zoom-in duration-300">
                  <Check className="h-2.5 w-2.5 text-white stroke-[4]" />
               </div>
            )}
            {isRejected && (
               <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-red-100 text-red-600 animate-in zoom-in duration-300">
                 RECHAZADA
               </span>
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
               variant="ghost" size="icon"
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToPrev}
               disabled={currentIdx === 0}
               aria-label="Factura anterior"
            >
               <ChevronLeft className="h-4 w-4 text-slate-400" />
            </Button>

            <div className="flex items-center gap-1.5 px-3">
               {Array.from({ length: totalVisible }).map((_, idx) => {
                  const inv = invoiceSummaries[idx];
                  if (!inv) {
                    return (
                      <span
                        key={`pending-${idx}`}
                        className="w-5 h-5 text-[9px] font-black rounded-full flex items-center justify-center text-slate-200 bg-slate-50 border border-slate-100"
                        title="Factura aún no procesada"
                      >
                        {idx + 1}
                      </span>
                    );
                  }
                  const done = approvedInvoices.has(inv.id);
                  const rejected = rejectedInvoices.has(inv.id);
                  const active = currentIdx === idx;
                  return (
                    <button
                      key={inv.id}
                      onClick={() => navigateTo(idx)}
                      className={cn(
                        "w-5 h-5 text-[9px] font-black rounded-full flex items-center justify-center transition-all",
                        active && !done && !rejected && "bg-slate-900 text-white scale-110 shadow-md",
                        !active && !done && !rejected && "hover:bg-slate-100 text-slate-300",
                        done && !active && "text-green-500 bg-green-50/50",
                        done && active && "bg-green-600 text-white scale-110 shadow-md",
                        rejected && !active && "text-red-400 bg-red-50/60",
                        rejected && active && "bg-red-500 text-white scale-110 shadow-md"
                      )}
                      aria-label={`Ir a factura ${inv.id}`}
                    >
                      {done && !active ? <Check className="h-2.5 w-2.5 stroke-[4]" /> : idx + 1}
                    </button>
                  );
               })}
            </div>

            <Button
               variant="ghost" size="icon"
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToNext}
               disabled={currentIdx === invoiceSummaries.length - 1}
               aria-label="Factura siguiente"
            >
               <ChevronRight className="h-4 w-4 text-slate-400" />
            </Button>
          </div>

          <Button
            variant="ghost" size="sm"
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
           {invoiceSummaries[currentIdx]?.folder_name && (
             <span
               className="text-[10px] text-slate-500 font-mono truncate max-w-[280px]"
               title={invoiceSummaries[currentIdx].folder_name}
             >
               {invoiceSummaries[currentIdx].folder_name}
             </span>
           )}
           <span className="text-[10px] font-black text-slate-300 uppercase tracking-widest font-mono">
             #{invoiceSummaries[currentIdx]?.id}
             {loadingDetail && (
               <Loader2 className="h-3 w-3 animate-spin inline ml-1 text-slate-400" />
             )}
           </span>
           {invoice?.doc_status && (
             <span className={cn(
               "text-[10px] font-bold px-2 py-1 rounded-full uppercase tracking-wider",
               invoice.doc_status === "done" && "bg-emerald-50 text-emerald-700 border border-emerald-200",
               invoice.doc_status === "confirmed" && "bg-emerald-100 text-emerald-800 border border-emerald-300",
               invoice.doc_status === "review" && "bg-amber-50 text-amber-700 border border-amber-200",
               invoice.doc_status === "blocked" && "bg-red-50 text-red-700 border border-red-200",
               invoice.doc_status === "cancelled" && "bg-orange-50 text-orange-700 border border-orange-200",
               invoice.doc_status === "error" && "bg-red-50 text-red-700 border border-red-200",
               invoice.doc_status === "processing" && "bg-slate-50 text-slate-600 border border-slate-200",
               invoice.doc_status === "uploaded" && "bg-blue-50 text-blue-700 border border-blue-200",
               invoice.doc_status === "retrying" && "bg-purple-50 text-purple-700 border border-purple-200",
             )}
             title="Estado del documento (sidecar .state.json)"
             >
               {invoice.doc_status}
             </span>
           )}
           {invoiceSummaries[currentIdx]?.duplicate_of && (
             <span
               className="text-[10px] font-bold px-2 py-1 rounded-full uppercase tracking-wider bg-yellow-50 text-yellow-800 border border-yellow-300"
               title={`Posible duplicado fiscal de '${invoiceSummaries[currentIdx]!.duplicate_of}' (mismo NIF emisor + nº factura + fecha)`}
             >
               ⚠ Duplicado
             </span>
           )}
           {invoice && (
             <span className={cn(
               "text-[10px] font-bold px-2 py-1 rounded-full",
               invoice.decision_global === "auto" && "bg-emerald-100 text-emerald-700",
               invoice.decision_global === "warn" && "bg-amber-100 text-amber-700",
               invoice.decision_global === "block" && "bg-red-100 text-red-700",
               invoice.decision_global === "pendiente" && "bg-slate-100 text-slate-600",
             )}
             title="Decisión automática del pipeline"
             >
               {invoice.decision_global?.toUpperCase()}
             </span>
           )}
           {(() => {
             const totalInvoices = invoiceSummaries.length;
             const decidedCount = approvedInvoices.size + rejectedInvoices.size;
             const allDecided = totalInvoices > 0 && decidedCount === totalInvoices;
             const hasApproved = approvedInvoices.size > 0;
             const ready = allDecided && hasApproved;
             const title = !hasApproved
               ? "Aprueba al menos una factura para exportar"
               : !allDecided
                 ? `Faltan ${totalInvoices - decidedCount} factura(s) por aceptar o rechazar`
                 : "Ir a exportar asientos";
             return (
               <Button
                 variant="outline" size="sm"
                 onClick={onExport}
                 disabled={!hasApproved}
                 className={cn(
                   "text-[10px] uppercase tracking-[0.15em] rounded-none transition-colors",
                   ready
                     ? "border-emerald-500 bg-emerald-500 text-white hover:bg-emerald-600 hover:text-white animate-pulse"
                     : "border-teal-200 text-teal-700 hover:bg-teal-50",
                   "disabled:opacity-40 disabled:cursor-not-allowed disabled:animate-none",
                 )}
                 title={title}
               >
                 Generar Asientos →
               </Button>
             );
           })()}
        </div>
      </header>

      {/* Main content */}
      <div className="flex flex-grow overflow-hidden border-b transition-opacity group-data-[approved=true]:opacity-90 group-data-[rejected=true]:opacity-80">
        <ResizablePanelGroup direction="horizontal">
          <ResizablePanel defaultSize={45} minSize={30}>
            <div className="h-full flex flex-col">
              <div className="p-4 border-b bg-slate-50 flex items-center justify-between flex-shrink-0">
                <h1 className="text-xs font-black uppercase tracking-widest text-slate-400">Datos Factura</h1>
              </div>
              <ScrollArea className="flex-grow">
                {invoice ? (
                <div className="p-10 space-y-10 max-w-2xl mx-auto">
                  {(() => {
                    const cliente = resolveClienteGestoria(invoice.libro, formData);
                    const libroLabel =
                      invoice.libro === "ingresos" ? "Emitida (ventas/ingresos)" :
                      invoice.libro === "gastos"   ? "Recibida (compras/gastos)" :
                      invoice.libro === "bienes"   ? "Bienes de inversión" : "—";
                    return (
                      <div className="sticky top-0 z-10 -mx-10 -mt-10 mb-4 px-10 py-4 bg-slate-50 border-b border-slate-200">
                        <div className="text-[10px] font-black uppercase tracking-[0.15em] text-slate-400">
                          Procesando factura para — {libroLabel}
                        </div>
                        <div className="text-base font-bold text-slate-800 mt-1">
                          {cliente.nombre || (
                            <span className="text-amber-600">⚠ Cliente no resuelto</span>
                          )}
                        </div>
                        <div className="text-xs text-slate-500 font-mono mt-0.5">
                          {cliente.nif || "—"}
                        </div>
                      </div>
                    );
                  })()}
                  <div className="grid grid-cols-2 gap-x-8 gap-y-6">
                    {invoice.fields.filter(f => !HEADER_FIELDS.has(f.id)).map((field) => (
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
                        {field.id === "cuenta_contable" ? (
                          <>
                            <Select
                              value={formData.cuenta_contable || ""}
                              onValueChange={(code) => {
                                const cuenta = CUENTA_BY_CODE[code];
                                dirtyRef.current = true;
                                setFormData((prev) => ({
                                  ...prev,
                                  cuenta_contable: code,
                                  concepto: cuenta ? cuenta.concepto : prev.concepto,
                                }));
                              }}
                            >
                              <SelectTrigger
                                id={field.id}
                                className={cn(
                                  "border-slate-100 focus:ring-0 focus:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all",
                                  field.status === "block" && "border-red-200 bg-red-50/30",
                                  field.status === "warn" && "border-amber-200",
                                )}
                              >
                                <SelectValue placeholder="Selecciona cuenta…" />
                              </SelectTrigger>
                              <SelectContent className="max-h-80">
                                {CUENTAS.map((c) => (
                                  <SelectItem
                                    key={c.code}
                                    value={c.code}
                                    className="font-mono text-xs"
                                  >
                                    <span className="font-bold mr-2">{c.code}</span>
                                    <span className="text-slate-600">{c.label}</span>
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {formData.concepto && (
                              <div className="text-[10px] font-mono text-slate-500 pl-1">
                                concepto: <span className="text-slate-700">{formData.concepto}</span>
                              </div>
                            )}
                          </>
                        ) : (
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
                        )}
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
                                 </tr>
                              </thead>
                              <tbody className="divide-y divide-slate-50">
                                 {fiscalLines.map((line, idx) => (
                                    <tr key={`${invoice.id}_line_${idx}`} className="hover:bg-slate-50/30 transition-colors">
                                       <td className="p-1 px-2">
                                          <div className="flex items-center">
                                             <Input value={typeof line.base === 'number' ? String(line.base).replace('.', ',') : String((line.base as unknown) ?? "")} onChange={(e) => handleLineChange(line.id, 'base', e.target.value)} className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs bg-transparent" />
                                             <span className="text-slate-200 pr-2">€</span>
                                          </div>
                                       </td>
                                       <td className="p-1 px-2">
                                          <div className="flex items-center justify-center">
                                             <Input
                                               value={line.vatRate === null || line.vatRate === 0 ? "" : String(line.vatRate).replace('.', ',')}
                                               placeholder="EXENTA"
                                               onChange={(e) => handleLineChange(line.id, 'vatRate', e.target.value)}
                                               className={cn(
                                                 "h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs text-center bg-transparent w-16",
                                                 (line.vatRate === null || line.vatRate === 0) && "placeholder:text-amber-500 placeholder:font-bold"
                                               )}
                                             />
                                             {line.vatRate !== null && line.vatRate !== 0 && (
                                               <span className="text-slate-200">%</span>
                                             )}
                                          </div>
                                       </td>
                                       <td className="p-1 px-2">
                                          <div className="flex items-center justify-end">
                                             <Input value={typeof line.vatAmount === 'number' ? String(line.vatAmount).replace('.', ',') : String((line.vatAmount as unknown) ?? "")} onChange={(e) => handleLineChange(line.id, 'vatAmount', e.target.value)} className="h-9 border-transparent focus-visible:border-slate-100 focus-visible:ring-0 rounded-none text-xs text-right bg-transparent" />
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
             <ImageViewer src={activeFileUrl} isLoading={!invoice} fileType={fileType} />
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>

      {/* Footer */}
      <footer className="h-24 flex items-center justify-center gap-6 px-8 bg-white flex-shrink-0">
        <Button
          variant="outline" size="lg"
          onClick={() => executeConfirmedAction("reject")}
          // Permitir re-aprobar/re-rechazar tras editar campos. El estilo
          // sigue indicando el estado actual (verde=aprobada, rojo=rechazada);
          // si el operario hace cambios y quiere reenviar, los botones le
          // dejan. Único guard real: ``submitting`` evita doble disparo durante
          // la llamada API en curso.
          disabled={!invoice || loadingDetail || submitting}
          className={cn(
            "w-52 h-11 font-bold uppercase text-[10px] tracking-[0.2em] rounded-none transition-all shadow-sm",
            isRejected
              ? "border-red-400 bg-red-50 text-red-600 hover:bg-red-100"
              : isApproved
              // Aprobada → click en Rechazar es válido (cambio de opinión).
              // Estilo más sutil para indicar que no es la acción primaria,
              // pero sigue accionable.
              ? "border-slate-200 bg-white text-slate-400 hover:bg-slate-50 hover:text-slate-700"
              : "border border-slate-200 hover:bg-slate-50 hover:text-slate-900"
          )}
        >
          {isRejected ? "Rechazada" : "Rechazar"}
        </Button>
        <Button
          size="lg"
          onClick={() => executeConfirmedAction("approve")}
          // Permitir re-aprobar/re-rechazar tras editar campos. El estilo
          // sigue indicando el estado actual (verde=aprobada, rojo=rechazada);
          // si el operario hace cambios y quiere reenviar, los botones le
          // dejan. Único guard real: ``submitting`` evita doble disparo durante
          // la llamada API en curso.
          disabled={!invoice || loadingDetail || submitting}
          className={cn(
            "w-52 h-11 font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            isApproved
              ? "bg-green-700 border-green-700 hover:bg-green-800 text-white"
              : isRejected
              // Rechazada → click en Aprobar es válido (cambio de opinión tras
              // edición). Estilo neutro accionable.
              ? "bg-slate-700 border-slate-700 hover:bg-slate-900 text-white"
              : "bg-slate-900 border border-slate-900 hover:bg-black text-white"
          )}
        >
          {isApproved ? (
            <><Check className="h-4 w-4 mr-2" />Aprobada</>
          ) : (
            <><Check className="h-4 w-4 mr-2" />Aprobar</>
          )}
        </Button>
      </footer>
    </div>
  );
}
