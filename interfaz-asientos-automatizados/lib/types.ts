export type FieldDecision = "auto" | "warn" | "block";

export interface InvoiceField {
  id: string;
  label: string;
  value: string;
  status: FieldDecision;
  confidence: number; // 0-100
  reason?: string;
  group: number;
  editable?: boolean;
}

export interface FiscalLine {
  id: string;
  base: number;
  vatRate: number;
  vatAmount: number;
  total: number;
  classification?: string;
  decisionLine?: string;
}

export type Libro = "gastos" | "ingresos" | "bienes";

/** Estado operativo del documento — último evento del .state.json (trazabilidad 2.0). */
export type DocStatus =
  | "uploaded"
  | "processing"
  | "retrying"
  | "review"
  | "done"
  | "confirmed"
  | "blocked"
  | "cancelled"
  | "error"
  | "renamed"
  | "reset"
  // Pre-scan Fase 1 (Gemini Vision) corrió en /upload y falló. Bloqueante:
  // requiere acción humana (retry-prescan u override-as-single) antes de
  // poder entrar al pipeline OCR.
  | "pre_scan_failed"
  // Sidecar original de un PDF multi-factura ya dividido — la trazabilidad
  // continúa en los doc_ids hijos. Terminal para esta carpeta.
  | "split";

/** Forma corta del libro — coincide con el prefijo del folder_name del asiento. */
export type LibroShort = "compras" | "ventas" | "bienes";

export interface InvoiceDocument {
  id: string;
  status: "pending" | "approved" | "rejected";
  imageUrl: string;
  fileType: "pdf" | "image";
  decision_global?: "auto" | "warn" | "block" | "pendiente";
  /** Estado del último evento del sidecar `.state.json`. */
  doc_status?: DocStatus;
  /** Nombre actual de la carpeta del asiento (renombrada o provisional). */
  folder_name?: string;
  fields: InvoiceField[];
  fiscalLines: FiscalLine[];
  libro?: Libro;
  /** Veces que el operario ha pulsado 'Rechazar' en este sidecar. */
  rejection_count?: number;
}

/** A file pending or already processed in a libro inbox.
 *
 * Trazabilidad 2.0: los PDFs viven permanentemente en
 * `libros/facturas/{libro_short}/` — ``status`` indica si ya tiene asiento
 * (procesado) o si sigue pendiente de pasar el pipeline.
 */
export interface BookFile {
  name: string;
  size_kb: number;
  added: string; // ISO8601
  /** ``null``/``undefined`` si la factura aún no ha sido procesada. */
  status?: DocStatus | null;
  /** Nombre de la carpeta de asiento si ya existe. */
  folder_name?: string | null;
  /** Resultado del pre-scan Fase 1 (presente solo en respuesta de /upload).
   *  No se persiste en /api/books: tras refrescar la página, la UI deriva
   *  el estado de cada archivo desde el sidecar (`status`). */
  pre_scan?: PreScanResult | null;
}

/** Hijo de un PDF multi-factura tal como lo creó el splitter Fase 1. */
export interface PreScanChild {
  doc_id: string;
  folder_name: string;
  status: string;
  /** Rango de páginas del PDF original (no siempre disponible en /api/books). */
  pages?: number[];
  emisor_cif?: string | null;
  confidence?: number | null;
}

/** Sub-objeto `pre_scan` por archivo en la respuesta de /upload. */
export interface PreScanResult {
  /** "single" = 1 factura. "split" = N facturas (ver `children`).
   *  "failed" = bloqueado, requiere acción humana.
   *  "skipped_non_pdf" = imagen, no se llama a Gemini.
   *  "skipped_disabled" = feature flag PRESCAN_ENABLED=false (legacy). */
  status: "single" | "split" | "failed" | "skipped_non_pdf" | "skipped_disabled";
  n_pages: number;
  detected_invoices: number;
  children: PreScanChild[];
  error: { kind: string; message: string; attempts: number } | null;
}

/** Resumen agregado del pre-scan para todo el lote subido. */
export interface DetectedSummary {
  total_invoices: number;
  files_ok: number;
  files_blocked: number;
  files_skipped: number;
}

/** An accounting book (gastos, ingresos, bienes) */
export interface Book {
  id: "gastos" | "ingresos" | "bienes";
  label: string;
  folder: string;
  /** Forma corta del libro en filesystem (``compras``/``ventas``/``bienes``). */
  libro_short?: LibroShort;
  files: BookFile[];
}

/** Pipeline processing status */
export interface PipelineStatus {
  status: "idle" | "running" | "completed" | "error";
  processed: number;
  total: number;
  current_file: string | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
}

/** Estado por factura mostrado en la vista en vivo de Fase 2/3. */
export type BatchFileStatus =
  | "pending"
  | "processing"
  | "done"
  | "review"
  | "confirmed"
  | "blocked"
  | "error";

/** Una factura dentro del lote actual (uno por doc_id de `.pending_confirm.json`). */
export interface BatchFileEntry {
  doc_id: string;
  filename: string;
  status: BatchFileStatus;
  folder_name?: string | null;
  decision?: string | null;
  /** Si la factura proviene del splitter (`X__NofM.pdf`), nombre original. */
  split_origin?: string | null;
  split_index?: number | null;
  split_total?: number | null;
}

/** Sección por libro de la respuesta de `/api/pipeline/batch`. */
export interface BatchBookSection {
  book_id: Libro;
  libro: LibroShort;
  label: string;
  files: BatchFileEntry[];
}

/** Composición del lote actual durante un run del pipeline. */
export interface PipelineBatch {
  in_flight: boolean;
  current_file: string | null;
  current_libro: LibroShort | null;
  books: BatchBookSection[];
}

/** A client from the gestoria's client registry */
export interface Client {
  nif: string;
  nombre: string;
  cuenta_contable: string;
}

/** Data stored for an approved invoice (used in export) */
export interface ApprovedInvoiceData {
  formData: Record<string, string>;
  fiscalLines: FiscalLine[];
  libro?: Libro;
  cuenta_contable: string;
}

// ──────────────────────────────────────────────────────────
// Trazabilidad por cliente (GET /api/clients, /api/clients/{nif}/invoices)
// ──────────────────────────────────────────────────────────

export interface ClientCard {
  nif: string;
  nombre: string;
  fecha_alta: string | null;
  ultima_factura_fecha: string | null;
  documentos_procesados: number;
  libros_activos: LibroShort[];
  tipos_activos: ("cliente" | "proveedor")[];
}

export interface ClientInvoice {
  doc_id: string;
  numero_factura: string | null;
  fecha_expedicion: string | null;
  fecha_operacion?: string | null;
  total_euros: number;
  decision_global: "auto" | "warn" | "block" | "pendiente" | null;
  status: DocStatus | null;
  libro: LibroShort | null;
  tiene_ediciones: boolean;
  lineas_asiento?: LineaAsiento[];
  contraparte_nif?: string | null;
  contraparte_nombre?: string | null;
  concepto?: string | null;
  cuenta_contable?: string | null;
  nif_entidad?: string | null;
  nombre_entidad?: string | null;
  nif_receptor?: string | null;
  nombre_receptor?: string | null;
  campos_editados?: string[];
}

// ──────────────────────────────────────────────────────────
// POST /api/pipeline/confirm — payload del batch confirm
// ──────────────────────────────────────────────────────────

export interface CampoFinal {
  valor: string | number | null;
}

export interface LineaAsiento {
  cuenta: string;
  concepto: string;
  debe: number;
  haber: number;
  tipo_iva?: number;
  base_imponible?: number;
}

export interface AsientoConfirm {
  campos_finales: Record<string, CampoFinal>;
  lineas_asiento: LineaAsiento[];
  csv_b64: string;
  /**
   * Override del libro decidido por el operario en fase 3. Si presente,
   * el backend lo prefiere sobre el derivado del nombre de carpeta del splitter.
   * Forma corta: "compras" | "ventas" | "bienes".
   */
  libro?: LibroShort;
}

export interface ConfirmBatchPayload {
  doc_ids: string[];
  asientos: Record<string, AsientoConfirm>;
}

export interface ConfirmBatchErrorEntry {
  doc_id: string;
  error: string;
  stage: string;
}

export interface ConfirmBatchResponse {
  /** True si TODOS los docs se confirmaron sin error. */
  ok: boolean;
  facturas_confirmadas: number;
  clientes_nuevos: number;
  /** PDFs del inbox eliminados tras la confirmación (uno por doc OK). */
  inbox_pdfs_deleted?: number;
  /** Errores parciales por doc — vacío en path feliz. */
  errors?: ConfirmBatchErrorEntry[];
}
