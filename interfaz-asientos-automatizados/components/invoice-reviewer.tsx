"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
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
import { Check, ChevronLeft, ChevronRight, Loader2, Plus, RefreshCw, X } from "lucide-react";
import { ApprovedInvoiceData, DocStatus, FiscalLine, InvoiceDocument, Libro, LibroShort } from "@/lib/types";
import {
  fetchInvoices,
  fetchInvoiceDetail,
  fetchPipelineBatch,
  sendInvoiceAction,
  deleteBookFile,
  transformToInvoice,
  API_URL,
} from "@/lib/api";
import { resolveClienteGestoria } from "@/lib/cliente-gestoria";
import { CUENTAS, CUENTA_BY_CODE } from "@/lib/cuentas-maestro";
import { inferLibroFromCuenta, libroLabel } from "@/lib/libro-inference";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const HEADER_FIELDS = new Set(["nif_cliente", "nombre_cliente", "concepto"]);

// Umbral del pager: por debajo se muestran dots numerados (uno por factura);
// por encima se cambia a control compacto "N / total" + input. Con 60 dots
// inline (≈1560 px) el header desbordaba y recortaba el botón "Generar
// Asientos →" por el overflow-hidden del wrapper raíz.
const PAGER_DOTS_MAX = 12;

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
  /** Veces rechazada por el operario (del listado backend). */
  rejection_count?: number;
}

interface InvoiceReviewerProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  rejectedInvoices: Set<string>;
  onApprove: (id: string, data: { formData: Record<string, string>; fiscalLines: FiscalLine[]; libro?: Libro }) => void;
  onReject: (id: string) => void;
  onExport: () => void;
  /** Total de facturas en cola en el pipeline (para mostrar dots pendientes al saltar a revisión anticipadamente). */
  totalQueued?: number;
  /** doc_ids a enfocar al cargar — set cuando el usuario llega vía toast "Abrir reviewer"
   *  tras resubir un PDF ya conocido con hijos pendientes. Posiciona el cursor en
   *  la primera factura que coincida. Se consume una sola vez y luego se limpia. */
  focusDocIds?: string[];
  /** Callback para que el padre limpie ``focusDocIds`` una vez aplicado. */
  onClearFocus?: () => void;
}

export function InvoiceReviewer({ approvedInvoices, rejectedInvoices, onApprove, onReject, onExport, totalQueued, focusDocIds, onClearFocus }: InvoiceReviewerProps) {
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
  // Confirmación de borrado definitivo (segundo rechazo): la factura ya volvió
  // una vez en el escaneo de carry-over; un segundo rechazo la elimina del todo.
  const [confirmHardDelete, setConfirmHardDelete] = useState<{ id: string; filename: string; bookId: Libro } | null>(null);
  // Texto del input del pager compacto. Vive como state local del input para
  // permitir borrar y reescribir libremente; el commit a currentIdx ocurre en
  // blur/Enter. Se resincroniza desde currentIdx vía useEffect.
  const [pageInputValue, setPageInputValue] = useState<string>("1");
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

  // Total real del lote según /api/pipeline/batch (post-splitter). Null hasta el
  // primer poll exitoso. Permite mostrar placeholders correctos cuando el
  // operario sube un PDF multi-factura: el splitter de Fase 1 genera N
  // sidecars `uploaded` y este endpoint los lista desde el primer momento,
  // incluso antes de que OCR los procese.
  const [batchTotal, setBatchTotal] = useState<number | null>(null);
  const batchTotalRef = useRef<number | null>(null);
  useEffect(() => { batchTotalRef.current = batchTotal; }, [batchTotal]);

  // Set de doc_ids del lote actual (post-splitter), derivado de /api/pipeline/batch.
  // Sirve para FILTRAR ``invoiceSummaries`` y mostrar SOLO las facturas del run en
  // curso — evita que facturas viejas en estados activos (review/blocked) de
  // escaneos anteriores se mezclen con las nuevas. Null hasta el primer poll.
  // Una vez poblado, se conserva aunque el backend devuelva in_flight=false
  // (típicamente al final del run, mientras el operario revisa antes de confirmar).
  const [batchDocIds, setBatchDocIds] = useState<Set<string> | null>(null);
  const batchDocIdsRef = useRef<Set<string> | null>(null);
  useEffect(() => { batchDocIdsRef.current = batchDocIds; }, [batchDocIds]);

  // True tras el primer poll a /api/pipeline/batch (éxito o error). Evita
  // pintar "No hay facturas en proceso" en la ventana inicial — entre que
  // loadInvoices resuelve (invoiceSummaries ya tiene datos) y el primer poll
  // del batch puebla batchDocIds — que dejaría visibleSummaries vacío de forma
  // espuria. Mientras !batchPolled mostramos el loading.
  const [batchPolled, setBatchPolled] = useState(false);

  useEffect(() => { currentIdxRef.current = currentIdx; }, [currentIdx]);
  // Resincronizar el texto del input del pager compacto cuando currentIdx
  // cambia por causas externas (navegación con flechas, auto-advance tras
  // aprobar). El usuario puede borrar/reescribir libremente entre tanto;
  // este efecto solo aplica cuando el cambio viene de fuera del input.
  useEffect(() => { setPageInputValue(String(currentIdx + 1)); }, [currentIdx]);
  useEffect(() => { invoiceSummariesRef.current = invoiceSummaries; }, [invoiceSummaries]);
  useEffect(() => { cacheRef.current = detailsCache; }, [detailsCache]);
  useEffect(() => { approvedInvoicesRef.current = approvedInvoices; }, [approvedInvoices]);

  const [formData, setFormData] = useState<Record<string, string>>({});
  const [fiscalLines, setFiscalLines] = useState<FiscalLine[]>([]);
  // Modo "cuenta + concepto personalizados" — activado al pulsar el botón "+",
  // o restaurado automáticamente al recargar una factura con un code que no
  // existe en el maestro (override guardado previamente).
  const [customCuentaMode, setCustomCuentaMode] = useState(false);
  // Libro efectivo de la factura actual. Inicialmente = invoice.libro del
  // splitter; puede cambiar si el operario edita la cuenta contable y el
  // prefijo PGC sugiere otro libro. El override viaja al backend en
  // AsientoConfirm.libro y se persiste en approvedInvoices al aprobar.
  const [effectiveLibro, setEffectiveLibro] = useState<Libro | undefined>(undefined);
  // Pulso visual en banner sticky tras un cambio automático (~1.5s).
  const [libroJustChanged, setLibroJustChanged] = useState(false);
  // Warning sutil bajo el campo cuenta cuando el prefijo no permite inferir
  // libro (ej. 4xx, 5xx, letra). Mensaje incluye el libro actual.
  const [cuentaWarning, setCuentaWarning] = useState<string | null>(null);
  // Timers cancelables: pulso del banner + debounce del toast en custom typing.
  const libroPulseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const customToastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  // Aplica ``focusDocIds`` una sola vez por valor recibido: tras cargar la lista,
  // posiciona el cursor en la primera factura cuyo id esté en la lista de focus
  // y limpia el focus en el padre. Si ningún id matchea (las facturas pueden
  // haber sido ya aprobadas/confirmadas), lo dejamos en silencio.
  //
  // Buscamos en ``invoiceSummaries`` (lista cruda) para asegurar match incluso
  // si el filtro de lote aún no se ha aplicado — y reseteamos currentIdx a 0
  // porque ``visibleSummaries`` con focusDocIds activo ya es solo las del set
  // (el currentIdx sobre la lista cruda no encajaría con el render).
  const focusAppliedRef = useRef<string[] | null>(null);
  useEffect(() => {
    if (!focusDocIds || focusDocIds.length === 0) return;
    if (invoiceSummaries.length === 0) return;
    // Identidad por contenido — no por referencia — para tolerar re-renders
    // del padre con el mismo array.
    const sig = focusDocIds.join(",");
    if (focusAppliedRef.current && focusAppliedRef.current.join(",") === sig) return;

    const targetSet = new Set(focusDocIds);
    const found = invoiceSummaries.some(
      (inv) => targetSet.has(inv.id) || (inv.folder_name ? targetSet.has(inv.folder_name) : false)
    );
    if (found) {
      // ``visibleSummaries`` ya filtra por focusDocIds, así que la primera
      // pendiente está en la posición 0.
      setCurrentIdx(0);
      const n = focusDocIds.length;
      toast.info(
        n === 1
          ? "Mostrando la factura pendiente del PDF resubido."
          : `Mostrando ${n} facturas pendientes del PDF resubido.`,
        { duration: 4000 }
      );
    }
    focusAppliedRef.current = focusDocIds;
    onClearFocus?.();
  }, [focusDocIds, invoiceSummaries, onClearFocus]);

  // Lista que se PINTA en el reviewer (filtrada por lote / focus).
  //
  // Prioridad:
  // 1. ``focusDocIds``: el usuario llegó vía "Abrir reviewer" tras resubir un
  //    PDF padre con hijos pendientes. Solo mostramos esas facturas.
  // 2. ``batchDocIds``: filtra a las facturas del lote en curso
  //    (``/api/pipeline/batch``). Evita que facturas viejas en estados activos
  //    (``review``/``blocked``/``done``) de runs anteriores contaminen el lote.
  // 3. Sin lote activo y sin focus: vacío. El JSX pinta un mensaje "vuelve a
  //    Gestión" en lugar de listar ghosts.
  //
  // ``invoiceSummaries`` sigue siendo la fuente cruda del backend (para cache,
  // matching de focus y `loadInvoices`). Solo la UI usa ``visibleSummaries``.
  const visibleSummaries = useMemo(() => {
    if (focusDocIds && focusDocIds.length > 0) {
      const fset = new Set(focusDocIds);
      return invoiceSummaries.filter(
        (inv) => fset.has(inv.id) || (inv.folder_name ? fset.has(inv.folder_name) : false)
      );
    }
    if (batchDocIds && batchDocIds.size > 0) {
      return invoiceSummaries.filter((inv) => batchDocIds.has(inv.id));
    }
    return [];
  }, [invoiceSummaries, focusDocIds, batchDocIds]);

  // Espejo en ref de la lista VISIBLE (filtrada). Los handlers async resuelven
  // la factura accionada por ``visibleSummariesRef.current[currentIdxRef.current]``
  // — exactamente como pinta el render (``visibleSummaries[currentIdx]``). Sin
  // esto, leer del array crudo ``invoiceSummaries`` con un ``currentIdx`` que
  // indexa la lista visible accionaba OTRA factura cuando el filtro de lote/focus
  // reducía la lista. Definido tras el useMemo para evitar TDZ en su dependencia.
  const visibleSummariesRef = useRef<InvoiceSummary[]>([]);
  useEffect(() => { visibleSummariesRef.current = visibleSummaries; }, [visibleSummaries]);

  // Clamp de currentIdx: si tras un merge / filtro la lista visible se redujo
  // (delete externo, reset mid-polling, batchDocIds cambia), evitamos quedar
  // fuera de rango. Apuntamos a la última factura disponible — preferimos no
  // saltar a 0 para no desorientar al usuario si solo cayeron las del final.
  useEffect(() => {
    if (visibleSummaries.length === 0) return;
    if (currentIdx >= visibleSummaries.length) {
      setCurrentIdx(visibleSummaries.length - 1);
    }
  }, [visibleSummaries.length, currentIdx]);

  // Polling a /api/pipeline/batch — fuente de verdad del total post-splitter.
  //
  // Se ejecuta mientras el lote esté `in_flight` (o no hayamos hecho aún el
  // primer poll). En cuanto el backend marca el lote como cerrado, hacemos
  // un último poll y paramos. Cualquier fallo se ignora silenciosamente para
  // no engañar al operario con errores transitorios.
  useEffect(() => {
    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | null = null;

    const poll = async () => {
      if (cancelled) return;
      try {
        const batch = await fetchPipelineBatch();
        if (cancelled) return;
        const sum = batch.books.reduce((acc, b) => acc + b.files.length, 0);
        // Solo actualizamos si crece — protege contra una respuesta vacía
        // tardía (in_flight=false con books=[]) que borraría placeholders
        // mientras /api/invoices aún no ha alcanzado el total real.
        setBatchTotal((prev) => (prev == null ? sum : Math.max(prev, sum)));

        // Set de doc_ids del lote: filtra el reviewer a SOLO las facturas
        // del run actual (evita ghosts de runs anteriores). Misma política
        // que batchTotal: solo actualizamos cuando llegan datos útiles, para
        // no borrar el set al final del run (in_flight=false) si el usuario
        // sigue revisando antes de confirmar.
        const ids = new Set<string>();
        for (const b of batch.books) {
          for (const f of b.files) ids.add(f.doc_id);
        }
        if (batch.in_flight || ids.size > 0) {
          setBatchDocIds((prev) => {
            if (!prev) return ids.size > 0 ? ids : null;
            // Unimos: si el lote crece (splitter añade hijos) se acumulan.
            // Si el backend deja de reportarlos (in_flight false post-confirm)
            // mantenemos el set anterior para que el operario pueda terminar.
            if (ids.size === 0) return prev;
            const merged = new Set(prev);
            ids.forEach((id) => merged.add(id));
            return merged.size > prev.size ? merged : prev;
          });
        }

        if (!batch.in_flight && interval) {
          clearInterval(interval);
          interval = null;
        }
      } catch {
        // Silencioso: el endpoint puede no estar listo o el server estar
        // reiniciando. El siguiente tick reintenta.
      } finally {
        // Marcamos que ya hicimos al menos un poll: a partir de aquí el
        // estado vacío del reviewer es real, no la ventana de carga inicial.
        if (!cancelled) setBatchPolled(true);
      }
    };

    poll();  // primer poll inmediato
    interval = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, []);

  // Polling para facturas pendientes de escaneo. Usa el max entre `totalQueued`
  // (pista de PipelineProgress.onJumpToReview, legacy) y `batchTotal`
  // (verdad post-splitter desde /api/pipeline/batch).
  //
  // Condiciones de salida:
  // 1. Hemos cargado al menos `target` facturas (pipeline completó).
  // 2. Máximo 30 intentos (~90s) — evita polling infinito si el pipeline
  //    falla o se cancela y nunca alcanza el target.
  // 3. Cleanup al unmount.
  useEffect(() => {
    const target = Math.max(batchTotal ?? 0, totalQueued ?? 0);
    if (target === 0) return;
    let attempts = 0;
    const MAX_ATTEMPTS = 30;
    const interval = setInterval(() => {
      const currentTarget = Math.max(batchTotalRef.current ?? 0, totalQueued ?? 0);
      if (currentTarget > 0 && invoiceSummariesRef.current.length >= currentTarget) {
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
  }, [totalQueued, batchTotal, loadInvoices]);

  useEffect(() => {
    const summary = visibleSummaries[currentIdx];
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
    setCustomCuentaMode(false);
    setEffectiveLibro(undefined);
    setCuentaWarning(null);
    setLibroJustChanged(false);
    if (libroPulseTimerRef.current) {
      clearTimeout(libroPulseTimerRef.current);
      libroPulseTimerRef.current = null;
    }
    if (customToastTimerRef.current) {
      clearTimeout(customToastTimerRef.current);
      customToastTimerRef.current = null;
    }
    dirtyRef.current = false;

    // Función auxiliar: aplica datos al formulario, priorizando datos aprobados por el usuario
    // sobre los datos brutos de la API (esto es el "human in the loop" — lo que el humano
    // aprobó es la fuente de verdad, no lo que escaneó la IA).
    const applyInvoiceData = (source: InvoiceDocument) => {
      const approvedData = approvedInvoicesRef.current.get(summary.id);
      let loadedFormData: Record<string, string>;
      if (approvedData) {
        // Factura ya aprobada: restaurar exactamente lo que el usuario aprobó
        loadedFormData = approvedData.formData;
        setFormData(approvedData.formData);
        setFiscalLines(approvedData.fiscalLines);
      } else {
        // Factura sin aprobar: cargar datos originales de la API
        const nextData: Record<string, string> = {};
        source.fields.forEach(f => { nextData[f.id] = f.value; });
        loadedFormData = nextData;
        setFormData(nextData);
        setFiscalLines(source.fiscalLines);
      }
      setFileUrl(source.imageUrl);
      setFileType(source.fileType);
      // Libro efectivo: si la factura ya fue aprobada con un override previo,
      // restauramos ese; sino usamos el del splitter (source.libro).
      setEffectiveLibro(approvedData?.libro ?? source.libro);
      // Restaurar modo custom si el code persistido no está en el maestro.
      const code = loadedFormData.cuenta_contable;
      setCustomCuentaMode(Boolean(code) && !(code in CUENTA_BY_CODE));
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
    // - visibleSummaries[currentIdx]?.id: fires si el summary actual cambia
    //   de id (raro — solo si backend reordena o el filtro batch cambia).
    // NO ponemos ``visibleSummaries`` entero porque el polling cada 3s lo
    // reemplaza con la misma lista y clobberaría el formData mientras el
    // usuario teclea. Bug reportado por operario en fase 3 con pipeline
    // todavía procesando.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentIdx, visibleSummaries[currentIdx]?.id]);

  const currentSummary = visibleSummaries[currentIdx];
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
    // Acota contra la lista VISIBLE (no la cruda): ``currentIdx`` indexa
    // ``visibleSummaries``, así que el límite superior es su longitud.
    const summaries = visibleSummariesRef.current;
    if (currentIdxRef.current < summaries.length - 1) navigateTo(currentIdxRef.current + 1);
  }, [navigateTo]);

  // Cleanup auto-advance on unmount
  useEffect(() => {
    return () => cancelAutoAdvance();
  }, [cancelAutoAdvance]);

  // Cleanup de timers de libro (pulso del banner + debounce del toast) en unmount
  useEffect(() => {
    return () => {
      if (libroPulseTimerRef.current) clearTimeout(libroPulseTimerRef.current);
      if (customToastTimerRef.current) clearTimeout(customToastTimerRef.current);
    };
  }, []);

  /**
   * Aplica la inferencia de libro a partir del código de cuenta contable.
   *
   *   - `select`:        cambio confirmado del Select del maestro → cambio inmediato + toast.
   *   - `custom-typing`: tecleo en el Input libre → cambio inmediato del libro
   *                      en banner; toast debounced 600 ms para no flashear
   *                      mientras se escribe "7" → "70" → "700".
   *   - `custom-blur`:   pérdida de foco del Input libre → consolida cualquier
   *                      cambio pendiente y muestra toast si lo hubo.
   *
   * Si el código es ambiguo (prefijo distinto de 2/6/7), no cambia el libro
   * pero muestra un warning sutil con el libro actualmente asignado.
   */
  const maybeSwitchLibro = useCallback(
    (code: string, source: "select" | "custom-typing" | "custom-blur") => {
      const trimmed = (code ?? "").trim();
      if (trimmed === "") {
        setCuentaWarning(null);
        if (customToastTimerRef.current) {
          clearTimeout(customToastTimerRef.current);
          customToastTimerRef.current = null;
        }
        return;
      }

      const inferred = inferLibroFromCuenta(trimmed);
      const current = effectiveLibro;

      if (inferred === null) {
        // Cuenta ambigua — no tocar libro, mostrar warning con el libro actual.
        setCuentaWarning(
          `Cuenta no reconocida. Libro actual: ${libroLabel(current)} (sin cambios).`,
        );
        return;
      }

      setCuentaWarning(null);

      if (inferred === current) return; // idempotente

      const previous = current;
      setEffectiveLibro(inferred);
      setLibroJustChanged(true);
      if (libroPulseTimerRef.current) clearTimeout(libroPulseTimerRef.current);
      libroPulseTimerRef.current = setTimeout(() => {
        setLibroJustChanged(false);
        libroPulseTimerRef.current = null;
      }, 1500);

      const showToast = () => {
        toast.info(
          `Libro cambiado: ${libroLabel(previous)} → ${libroLabel(inferred)}`,
          {
            description: `Cuenta ${trimmed} reasignó cliente y contraparte automáticamente.`,
            action: {
              label: "Deshacer",
              onClick: () => setEffectiveLibro(previous),
            },
            duration: 6000,
          },
        );
      };

      if (source === "custom-typing") {
        // Debounce: tipear "6"→"60"→"600" no debe flashear toasts.
        if (customToastTimerRef.current) clearTimeout(customToastTimerRef.current);
        customToastTimerRef.current = setTimeout(() => {
          showToast();
          customToastTimerRef.current = null;
        }, 600);
      } else {
        // select / custom-blur → toast inmediato.
        if (customToastTimerRef.current) {
          clearTimeout(customToastTimerRef.current);
          customToastTimerRef.current = null;
        }
        showToast();
      }
    },
    [effectiveLibro],
  );

  // Ejecutar la acción (aprobar/rechazar) con guard de doble-click
  const executeConfirmedAction = async (action: "approve" | "reject") => {
    // Guard síncrono: si dos clicks llegan en el mismo tick antes de que
    // React propague setSubmitting(true), el segundo ve submittingRef=true.
    if (submittingRef.current) return;
    // Resolvemos la factura accionada desde la lista VISIBLE (filtrada), igual
    // que el render (``visibleSummaries[currentIdx]``). Leer del array crudo
    // ``invoiceSummaries`` accionaba otra factura cuando focus/lote filtraba.
    const currentSummaryForAction = visibleSummariesRef.current[currentIdxRef.current];
    const currentInvoice = detailsCache.get(currentSummaryForAction?.id);
    if (!currentInvoice) return;

    // Segundo rechazo → diálogo de borrado definitivo (no se llama al backend
    // reject: la factura ya vivía en `review` desde el primer rechazo + carry-over).
    if (action === "reject") {
      // `rejection_count` de la MISMA factura mostrada (el summary visible),
      // igual que el label del botón.
      const summary = currentSummaryForAction;
      const previousRejects = summary?.rejection_count ?? 0;
      if (previousRejects >= 1) {
        if (!currentInvoice.libro) {
          toast.error("No se pudo determinar el libro de la factura — recarga e intenta de nuevo.");
          return;
        }
        setConfirmHardDelete({
          id: currentInvoice.id,
          filename: currentInvoice.invoice_filename ?? `${currentInvoice.id}.pdf`,
          bookId: currentInvoice.libro,
        });
        return;  // espera confirmación del diálogo; sin submit, sin auto-advance.
      }
    }

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
        // Persistimos el libro EFECTIVO (puede diferir del splitter si el
        // operario lo corrigió cambiando la cuenta contable).
        onApprove(currentInvoice.id, {
          formData,
          fiscalLines,
          libro: effectiveLibro ?? currentInvoice.libro,
        });
        toast.success("Factura aprobada correctamente");
      } else {
        onReject(currentInvoice.id);
        toast.info("Factura rechazada. Volverá a aparecer en el próximo escaneo.");
      }

      // Auto-advance solo si el usuario NO ha navegado durante el await.
      // Acota contra la lista VISIBLE (currentIdx la indexa), no la cruda.
      if (
        currentIdxRef.current === idxAtSubmit &&
        currentIdxRef.current < visibleSummariesRef.current.length - 1
      ) {
        autoAdvanceRef.current = setTimeout(() => {
          const summaries = visibleSummariesRef.current;
          const idx = currentIdxRef.current;
          // Re-chequear: si el usuario navegó dentro de los 500ms, no avanzamos.
          if (idx === idxAtSubmit && idx < summaries.length - 1) {
            setCurrentIdx(idx + 1);
          }
          autoAdvanceRef.current = null;
        }, 500);
      }
    } catch (err) {
      toast.error(`Error al ${action === "approve" ? "aprobar" : "rechazar"} la factura`);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const handleRefresh = () => loadInvoices(true);

  // Loading / Error states.
  // Además del `loading` de la carga inicial, seguimos mostrando el spinner
  // mientras haya facturas crudas pero el filtro de lote aún no se haya
  // resuelto (primer poll del batch pendiente). Sin esto, se pinta un falso
  // "No hay facturas en proceso" en la ventana entre que /api/invoices
  // responde y /api/pipeline/batch puebla `batchDocIds`.
  const awaitingBatchFilter =
    !error && !batchPolled && invoiceSummaries.length > 0 && visibleSummaries.length === 0;
  if (loading || awaitingBatchFilter) {
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

  if (error || visibleSummaries.length === 0) {
    // Mensaje distinto según haya backend con facturas crudas o lote vacío:
    // - error o sin facturas crudas: el clásico "no hay facturas, reintentar".
    // - facturas crudas pero filtradas a 0: el lote acabó / no hay lote en
    //   curso. Indicamos al operario que vuelva a Gestión a subir.
    const hasRawButFiltered = !error && invoiceSummaries.length > 0;
    return (
      // FIX #3: h-full en lugar de h-screen
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">
            {error
              ?? (hasRawButFiltered
                ? "No hay facturas en proceso. Vuelve a Gestión y sube un PDF para empezar un nuevo escaneo."
                : "No hay facturas disponibles")}
          </p>
          <Button onClick={() => loadInvoices()} variant="outline" className="text-xs">
            Reintentar
          </Button>
        </div>
      </div>
    );
  }

  const invoice = detailsCache.get(visibleSummaries[currentIdx]?.id);
  const isApproved = invoice ? approvedInvoices.has(invoice.id) : false;
  const isRejected = invoice ? rejectedInvoices.has(invoice.id) : false;
  // Segundo rechazo: si la factura ya fue rechazada en un escaneo previo, el
  // botón "Rechazar" pasa a "Eliminar definitivamente" (hard delete).
  const currentRejectionCount = visibleSummaries[currentIdx]?.rejection_count ?? 0;
  const isSecondReject = currentRejectionCount >= 1;
  // `effectiveTotal` es el max de: total real del lote post-splitter (batchTotal),
  // pista legacy de PipelineProgress (totalQueued), y facturas ya visibles. Esto
  // garantiza placeholders correctos para PDFs multi-factura (batchTotal manda),
  // y no muestra placeholders fantasma cuando el reviewer entra fuera de un run
  // (todos en 0 → cae a visibleSummaries.length).
  const effectiveTotal = Math.max(
    batchTotal ?? 0,
    totalQueued ?? 0,
    visibleSummaries.length,
  );
  const totalVisible = effectiveTotal;
  const totalInvoices = totalVisible;
  const loadedCount = visibleSummaries.length;
  const approvedCount = visibleSummaries.filter(inv => approvedInvoices.has(inv.id)).length;

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

      {/* AlertDialog para borrado definitivo (segundo rechazo) */}
      <AlertDialog open={confirmHardDelete !== null} onOpenChange={(open) => { if (!open) setConfirmHardDelete(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Eliminar definitivamente</AlertDialogTitle>
            <AlertDialogDescription>
              Esta factura ya fue rechazada en un escaneo anterior. Al confirmar se
              eliminará por completo del sistema — el PDF y todos sus datos. No queda
              rastro y no volverá a aparecer.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setConfirmHardDelete(null)}>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              className="bg-red-600 hover:bg-red-700"
              onClick={async () => {
                if (!confirmHardDelete) return;
                const { id, filename, bookId } = confirmHardDelete;
                try {
                  await deleteBookFile(bookId, filename);
                  onReject(id);  // saca del estado de UI (export, etc.)
                  // Quita de la lista local para que avance de inmediato sin esperar al poll.
                  setInvoiceSummaries((prev) => prev.filter((s) => s.id !== id));
                  toast.success("Factura eliminada definitivamente");
                } catch (err) {
                  const msg = err instanceof Error ? err.message : String(err);
                  // El hard delete pasa por /api/books DELETE, que devuelve 409
                  // si el pipeline sigue corriendo (posible al entrar al reviewer
                  // anticipadamente con el CTA "Empezar a revisar"). Traducimos a
                  // un mensaje claro en vez de exponer el texto crudo del backend.
                  if (/pipeline is running/i.test(msg)) {
                    toast.error("El escaneo sigue en curso — espera a que termine para eliminar.");
                  } else {
                    toast.error(`Error eliminando: ${msg}`);
                  }
                } finally {
                  setConfirmHardDelete(null);
                }
              }}
            >
              Eliminar definitivamente
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
        <div className="flex items-center gap-4 min-w-0">
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

            {totalVisible <= PAGER_DOTS_MAX ? (
              <div className="flex items-center gap-1.5 px-3">
                 {Array.from({ length: totalVisible }).map((_, idx) => {
                    const inv = visibleSummaries[idx];
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
            ) : (
              <div className="flex items-center gap-2 px-3">
                <div className="text-[11px] font-mono font-bold text-slate-600 tabular-nums flex items-center gap-1">
                  <input
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    value={pageInputValue}
                    onChange={(e) => {
                      // Solo dígitos; permite cadena vacía para reescribir.
                      setPageInputValue(e.target.value.replace(/\D/g, ""));
                    }}
                    onBlur={() => {
                      const n = parseInt(pageInputValue, 10);
                      if (!Number.isNaN(n) && n >= 1 && n <= totalVisible) {
                        navigateTo(n - 1);
                      } else {
                        // Inválido o vacío: descartar y restaurar al currentIdx.
                        setPageInputValue(String(currentIdx + 1));
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        (e.target as HTMLInputElement).blur();
                      } else if (e.key === "Escape") {
                        setPageInputValue(String(currentIdx + 1));
                        (e.target as HTMLInputElement).blur();
                      }
                    }}
                    className="w-10 text-center bg-transparent border-b border-slate-200 focus:outline-none focus:border-teal-500"
                    aria-label="Ir a factura número"
                  />
                  <span className="text-slate-400">/</span>
                  <span>{totalVisible}</span>
                </div>
                {approvedCount > 0 && (
                  <span className="text-[10px] text-emerald-500 font-bold">
                    ({approvedCount} ✓)
                  </span>
                )}
              </div>
            )}

            <Button
               variant="ghost" size="icon"
               className="h-7 w-7 hover:bg-slate-100 rounded-none"
               onClick={goToNext}
               disabled={currentIdx === visibleSummaries.length - 1}
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
        <div className="flex items-center gap-3 flex-shrink-0">
           {visibleSummaries[currentIdx]?.folder_name && (
             <span
               className="text-[10px] text-slate-500 font-mono truncate max-w-[280px]"
               title={visibleSummaries[currentIdx].folder_name}
             >
               {visibleSummaries[currentIdx].folder_name}
             </span>
           )}
           <span className="text-[10px] font-black text-slate-300 uppercase tracking-widest font-mono">
             #{visibleSummaries[currentIdx]?.id}
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
           {visibleSummaries[currentIdx]?.duplicate_of && (
             <span
               className="text-[10px] font-bold px-2 py-1 rounded-full uppercase tracking-wider bg-yellow-50 text-yellow-800 border border-yellow-300"
               title={`Posible duplicado fiscal de '${visibleSummaries[currentIdx]!.duplicate_of}' (mismo NIF emisor + nº factura + fecha)`}
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
             // Usa `effectiveTotal` del scope externo (max de batchTotal,
             // totalQueued e invoiceSummaries.length) — el total real del lote
             // post-splitter, no solo las facturas ya cargadas en memoria.
             // Sin esto, en lotes multi-factura el botón se ponía "ready"
             // prematuramente al aprobar las cargadas mientras OCR seguía.
             const decidedCount = approvedInvoices.size + rejectedInvoices.size;
             const allDecided = effectiveTotal > 0 && decidedCount === effectiveTotal;
             const hasApproved = approvedInvoices.size > 0;
             const ready = allDecided && hasApproved;
             const title = !hasApproved
               ? "Aprueba al menos una factura para exportar"
               : !allDecided
                 ? `Faltan ${effectiveTotal - decidedCount} factura(s) por aceptar o rechazar`
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
                    const cliente = resolveClienteGestoria(effectiveLibro, formData);
                    const labelActual = libroLabel(effectiveLibro);
                    const isOverride =
                      invoice.libro !== undefined &&
                      effectiveLibro !== undefined &&
                      effectiveLibro !== invoice.libro;
                    return (
                      <div
                        className={cn(
                          "sticky top-0 z-10 -mx-10 -mt-10 mb-4 px-10 py-4 bg-slate-50 border-b border-slate-200 transition-all",
                          libroJustChanged && "ring-2 ring-teal-400",
                        )}
                      >
                        <div className="text-[10px] font-black uppercase tracking-[0.15em] text-slate-400">
                          Procesando factura para — {labelActual}
                        </div>
                        {isOverride && (
                          <div className="text-[10px] text-slate-400 mt-0.5 font-mono">
                            original: {libroLabel(invoice.libro)}
                          </div>
                        )}
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
                          {customCuentaMode ? (
                            <>
                              <div className="flex items-center gap-1">
                                <Input
                                  id={field.id}
                                  value={formData.cuenta_contable || ""}
                                  onChange={(e) => {
                                    handleInputChange("cuenta_contable", e.target.value);
                                    maybeSwitchLibro(e.target.value, "custom-typing");
                                  }}
                                  onBlur={(e) =>
                                    maybeSwitchLibro(e.target.value, "custom-blur")
                                  }
                                  placeholder="Cuenta contable (libre)"
                                  className={cn(
                                    "border-slate-100 focus-visible:ring-0 focus-visible:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all focus-visible:shadow-sm uppercase",
                                    field.status === "block" && "border-red-200 bg-red-50/30",
                                    field.status === "warn" && "border-amber-200",
                                  )}
                                />
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-slate-400 hover:text-slate-700 shrink-0"
                                  title="Volver al desplegable del maestro"
                                  onClick={() => setCustomCuentaMode(false)}
                                >
                                  <X className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                              <Input
                                value={formData.concepto || ""}
                                onChange={(e) => handleInputChange("concepto", e.target.value)}
                                placeholder="Concepto (snake_case sugerido)"
                                className="border-slate-100 focus-visible:ring-0 focus-visible:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all focus-visible:shadow-sm uppercase"
                              />
                              <div className="text-[10px] font-mono text-slate-400 pl-1">
                                modo personalizado — fuera del maestro
                              </div>
                            </>
                          ) : (
                            <>
                              <div className="flex items-center gap-1">
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
                                    maybeSwitchLibro(code, "select");
                                  }}
                                >
                                  <SelectTrigger
                                    id={field.id}
                                    className={cn(
                                      "border-slate-100 focus:ring-0 focus:border-slate-400 rounded-none h-10 font-mono text-xs shadow-none bg-white transition-all flex-1 uppercase",
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
                                        className="font-mono text-xs uppercase"
                                      >
                                        <span className="font-bold mr-2">{c.code}</span>
                                        <span className="text-slate-600">{c.label}</span>
                                      </SelectItem>
                                    ))}
                                  </SelectContent>
                                </Select>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 text-slate-400 hover:text-slate-700 shrink-0"
                                  title="Cuenta + concepto personalizados"
                                  onClick={() => {
                                    dirtyRef.current = true;
                                    setCustomCuentaMode(true);
                                  }}
                                >
                                  <Plus className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                              {formData.concepto && (
                                <div className="text-[10px] font-mono text-slate-500 pl-1">
                                  concepto: <span className="text-slate-700 uppercase">{formData.concepto}</span>
                                </div>
                              )}
                            </>
                          )}
                          {cuentaWarning && (
                            <p className="text-[10px] text-amber-600 mt-1 pl-1 font-mono">
                              {cuentaWarning}
                            </p>
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
      <footer className="sticky bottom-0 z-20 h-24 flex items-center justify-center gap-6 px-8 bg-white border-t border-slate-100 flex-shrink-0 shadow-[0_-2px_8px_-2px_rgba(0,0,0,0.06)]">
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
          {isRejected ? "Rechazada" : isSecondReject ? "Eliminar definitivamente" : "Rechazar"}
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
