/**
 * API client for communicating with the pipeline backend.
 */

import type {
  InvoiceDocument,
  FieldDecision,
  Book,
  PipelineStatus,
  Client,
  Libro,
  DocStatus,
  LibroShort,
} from "./types";

/**
 * Mapeo libro largo (`20_COMPRAS_GASTOS`) → libro frontend (`gastos`).
 * Se conserva como fallback para `metadata.libro` (legacy). El backend
 * trazabilidad 2.0 ya devuelve directamente la forma corta en `invoice.libro`
 * (`compras`/`ventas`/`bienes`), que mapeamos abajo a la forma frontend.
 */
const LIBRO_LONG_MAP: Record<string, Libro> = {
  "20_COMPRAS_GASTOS": "gastos",
  "21_VENTAS_INGRESOS": "ingresos",
  "22_BIENES_INVERSION": "bienes",
};

/**
 * Mapeo libro corto del filesystem (`compras`) → libro del frontend (`gastos`).
 * Es el contrato nuevo del API trazabilidad 2.0.
 */
const LIBRO_SHORT_MAP: Record<LibroShort, Libro> = {
  compras: "gastos",
  ventas: "ingresos",
  bienes: "bienes",
};

/**
 * Base URL vacío — todas las rutas /api/* son relativas al origen del navegador.
 * Next.js hace proxy interno a pipeline-api:8000 via rewrites en next.config.mjs.
 * Esto elimina la dependencia de NEXT_PUBLIC_API_URL y permite cambiar la IP
 * del servidor sin reconstruir la imagen Docker.
 */
export const API_URL = "";

const API_TIMEOUT_MS = 30000;

async function fetchWithTimeout(url: string, options?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    return response;
  } finally {
    clearTimeout(timeoutId);
  }
}

export interface FieldStatus {
  valor_final: string | number;
  decision: "auto" | "warn" | "block" | "pendiente" | "error";
  confianza: number;
  motivo?: string;
  fuente_modulo?: string;
  fuente_dato?: string;
  llm_usado?: boolean;
  ocr_fallback?: boolean;
}

/** API format fiscal line (from backend) */
interface ApiFiscalLine {
  id?: string;
  base?: number;
  tipo_iva?: number;
  cuota?: number;
  total?: number;
  classification?: string;
  clasificacion?: string;
  decisionLine?: string;
  decision_linea?: string;
}

export interface InvoiceListItem {
  id: string;
  /** Nombre actual de la carpeta de asiento (operario-friendly tras rename). */
  folder_name?: string;
  /** Forma corta del libro (`compras`/`ventas`/`bienes`). */
  libro?: LibroShort | null;
  /** Estado actual del documento (último evento del sidecar). */
  status?: DocStatus | null;
  decision_global: string;
  timestamp: string;
  nif_entidad: string;
  nombre_entidad: string;
  numero_factura: string;
  total_euros: number;
}

export interface InvoiceListResponse {
  invoices: InvoiceListItem[];
  total: number;
}

/** Evento de la línea de vida del documento (sidecar `.state.json`). */
export interface DocEvent {
  ts: string;
  status: DocStatus;
  [key: string]: unknown;
}

export interface InvoiceDetailResponse {
  id: string;
  folder_name?: string;
  libro?: LibroShort | null;
  status?: DocStatus | null;
  decision_global: string;
  metadata: { fecha_ensamblado?: string; libro?: string } & Record<string, unknown>;
  fields: Record<string, FieldStatus>;
  fiscal_lines: ApiFiscalLine[];
  artifacts: string[];
  file_url?: string;
  invoice_filename?: string;
  /** Línea de vida del documento (orden cronológico, append-only). */
  events?: DocEvent[];
}

/**
 * Fetch the list of processed invoices.
 */
export async function fetchInvoices(): Promise<InvoiceListResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/invoices`);

    if (!response.ok) {
      throw new Error(`Failed to fetch invoices: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Fetch detailed information for a specific invoice.
 */
export async function fetchInvoiceDetail(docId: string): Promise<InvoiceDetailResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/invoices/${docId}`);

    if (!response.ok) {
      if (response.status === 404) {
        throw new Error(`Invoice ${docId} not found`);
      }
      throw new Error(`Failed to fetch invoice: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Transform API response to UI-compatible InvoiceDocument.
 */
export function transformToInvoice(detail: InvoiceDetailResponse, imageUrl?: string): InvoiceDocument {
  const fieldMapping: Record<string, { label: string; group: number }> = {
    nif_entidad: { label: "NIF Entidad", group: 1 },
    nombre_entidad: { label: "Nombre Entidad", group: 1 },
    numero_factura: { label: "Nº Factura", group: 2 },
    fecha_expedicion: { label: "Fecha Expedición", group: 2 },
    fecha_operacion: { label: "Fecha Operación", group: 2 },
    total_euros: { label: "Total €", group: 3 },
    nif_receptor: { label: "NIF Receptor", group: 5 },
    nombre_receptor: { label: "Nombre Receptor", group: 5 },
    concepto: { label: "Concepto", group: 4 },
    cuenta_contable: { label: "Cuenta PGC", group: 4 },
    nif_cliente: { label: "NIF Cliente", group: 5 },
    nombre_cliente: { label: "Nombre Cliente", group: 5 },
  };

  const mandatoryFields = new Set([
    "nif_entidad", "nombre_entidad", "numero_factura", "fecha_expedicion",
    "total_euros", "nif_receptor", "concepto", "cuenta_contable"
  ]);

  const fields = Object.entries(detail.fields)
    .filter(([_, value]) => value && value.valor_final !== undefined)
    .map(([key, value]) => {
      const mapping = fieldMapping[key] || { label: key, group: 99 };
      return {
        id: key,
        label: mapping.label,
        value: String(value.valor_final || ""),
        status: (value.decision as FieldDecision) || "pendiente",
        confidence: Math.round((value.confianza || 0) * 100),
        reason: value.motivo,
        group: mapping.group,
        editable: value.decision !== "auto",
      };
    });

  // Add mandatory fields that are missing (with empty values)
  const existingIds = new Set(fields.map(f => f.id));
  for (const fieldId of mandatoryFields) {
    if (!existingIds.has(fieldId)) {
      const mapping = fieldMapping[fieldId] || { label: fieldId, group: 99 };
      fields.push({
        id: fieldId,
        label: mapping.label,
        value: "",
        status: "pendiente" as FieldDecision,
        confidence: 0,
        reason: "Campo no encontrado por el pipeline",
        group: mapping.group,
        editable: true,
      });
    }
  }

  // Sort by group then by id for consistent display order
  fields.sort((a, b) => a.group - b.group || a.id.localeCompare(b.id));

  // Transform fiscal lines from API format to UI format
  const fiscalLines = (detail.fiscal_lines || []).map((line, idx) => ({
    id: line.id || `line_${idx}`,
    base: line.base || 0,
    vatRate: line.tipo_iva || 0,
    vatAmount: line.cuota || 0,
    total: line.total || 0,
    classification: line.classification || line.clasificacion || "",
    decisionLine: line.decisionLine || line.decision_linea || "",
  }));

  // Use actual file URL if available, otherwise fallback to placeholder
  const fileUrl = detail.file_url || `${API_URL}/api/invoices/${detail.id}/file`;

  // Determine file type from filename for proper rendering
  const filename = detail.invoice_filename || "";
  const fileType: "pdf" | "image" = filename.toLowerCase().endsWith(".pdf") ? "pdf" : "image";

  // Trazabilidad 2.0: el backend ya devuelve `libro` en forma corta
  // (`compras`/`ventas`/`bienes`). Fallback al mapping legacy desde
  // `metadata.libro` (`20_COMPRAS_GASTOS`) para compatibilidad.
  const libroShort = detail.libro ?? null;
  const libroLegacy = detail.metadata?.libro as string | undefined;
  const libro: Libro | undefined = libroShort
    ? LIBRO_SHORT_MAP[libroShort]
    : libroLegacy
      ? LIBRO_LONG_MAP[libroLegacy]
      : undefined;

  return {
    id: detail.id,
    status: detail.decision_global === "auto" ? "approved" : "pending",
    imageUrl: imageUrl || fileUrl,
    fileType,
    decision_global: detail.decision_global as InvoiceDocument["decision_global"],
    doc_status: detail.status ?? undefined,
    folder_name: detail.folder_name,
    fields,
    fiscalLines,
    libro,
  };
}

/**
 * Send an action (approve/reject) for an invoice.
 *
 * Trazabilidad 2.0: el backend registra la acción como evento en el
 * sidecar `.state.json` del asiento; no se mueve el PDF. La respuesta
 * incluye `new_status` (``done`` para approve, ``review`` para reject).
 */
export interface InvoiceActionResponse {
  status: "success";
  message: string;
  doc_id: string;
  folder_name: string;
  new_status: DocStatus;
}

export async function sendInvoiceAction(
  docId: string,
  action: "approve" | "reject",
  corrections?: {
    fields?: Record<string, string>;
    fiscalLines?: Array<{ base: number; tipo_iva: number | null; cuota: number; total: number }>;
  }
): Promise<InvoiceActionResponse> {
  const payload: Record<string, any> = {
    action,
    document_id: docId,
  };

  if (corrections) {
    if (corrections.fields) payload.corrections_fields = corrections.fields;
    if (corrections.fiscalLines) payload.corrections_fiscal_lines = corrections.fiscalLines;
  }

  try {
    const response = await fetchWithTimeout(`${API_URL}/api/invoices/${docId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`Failed to ${action} invoice ${docId}: ${error}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Fetch pipeline statistics.
 */
export async function fetchStats(): Promise<{
  total: number;
  by_decision: {
    auto: number;
    warn: number;
    pendiente: number;
    block: number;
  };
  by_user_action: {
    approved: number;
    rejected: number;
  };
}> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/stats`);

    if (!response.ok) {
      throw new Error(`Failed to fetch stats: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/** Types for new API functions */
export interface BooksResponse {
  books: Book[];
}

export interface UploadResponse {
  uploaded: string[];
  errors: Array<{ file: string; error: string }>;
}

export interface PipelineRunResponse {
  status: string;
  total: number;
}

export interface ClientsResponse {
  clients: Client[];
}

/**
 * Fetch the list of accounting books with pending files.
 */
export async function fetchBooks(): Promise<BooksResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/books`);
    if (!response.ok) {
      throw new Error(`Failed to fetch books: ${response.statusText}`);
    }
    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Upload files to a specific accounting book.
 */
export async function uploadFiles(
  bookId: "gastos" | "ingresos" | "bienes",
  files: File[]
): Promise<UploadResponse> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));

  try {
    const response = await fetchWithTimeout(`${API_URL}/api/books/${bookId}/upload`, {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      let errorMessage = `Error subiendo archivos (${response.status})`;
      try {
        const errorBody = await response.json();
        if (errorBody.detail) errorMessage = errorBody.detail;
      } catch {
        const errorText = await response.text();
        if (errorText) errorMessage = errorText;
      }
      throw new Error(errorMessage);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Delete a pending file from a book before the pipeline starts.
 */
export async function deleteBookFile(
  bookId: "gastos" | "ingresos" | "bienes",
  filename: string
): Promise<void> {
  const encodedName = encodeURIComponent(filename);
  const response = await fetchWithTimeout(
    `${API_URL}/api/books/${bookId}/files/${encodedName}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    let msg = `Error eliminando archivo (${response.status})`;
    try {
      const body = await response.json();
      if (body.detail) msg = body.detail;
    } catch { /* ignore */ }
    throw new Error(msg);
  }
}

/**
 * Fetch current pipeline processing status.
 */
export async function fetchPipelineStatus(): Promise<PipelineStatus> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/pipeline/status`);
    if (!response.ok) {
      throw new Error(`Failed to fetch pipeline status: ${response.statusText}`);
    }
    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Trigger pipeline processing.
 */
export async function runPipeline(): Promise<PipelineRunResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/pipeline/run`, {
      method: "POST",
    });

    if (!response.ok) {
      const errorText = await response.text();
      if (response.status === 409) {
        throw new Error("PIPELINE_ALREADY_RUNNING");
      }
      if (response.status === 500) {
        throw new Error("Error interno del servidor. Revisa los logs del backend.");
      }
      if (response.status === 422) {
        throw new Error("Datos de entrada inválidos. Revisa los archivos subidos.");
      }
      throw new Error(`Error del pipeline (${response.status}): ${errorText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Reset pipeline (trazabilidad 2.0): borra todas las carpetas de asiento
 * y el estado runtime. Los PDFs originales no se tocan — siguen en su inbox
 * permanente listos para reproceso.
 */
export interface ResetPipelineResponse {
  status: string;
  asientos_deleted: number;
}

export async function resetPipeline(): Promise<ResetPipelineResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/pipeline/reset`, {
      method: "POST",
    });
    if (!response.ok) {
      const errorText = await response.text();
      if (response.status === 409) {
        throw new Error("Pipeline en ejecución. Espera a que termine antes de resetear.");
      }
      throw new Error(`Error reseteando pipeline: ${errorText}`);
    }
    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Timeout reseteando pipeline.");
    }
    throw error;
  }
}

/**
 * Fetch registered clients from the gestoria.
 */
export async function fetchClients(): Promise<ClientsResponse> {
  try {
    const response = await fetchWithTimeout(`${API_URL}/api/clients`);
    if (!response.ok) {
      throw new Error(`Failed to fetch clients: ${response.statusText}`);
    }
    return response.json();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server may still be processing.");
    }
    throw error;
  }
}

/**
 * Cancel an in-flight pipeline run (cooperative — stops after current invoice).
 */
export async function cancelPipeline(): Promise<{ status: string }> {
  const response = await fetchWithTimeout(`${API_URL}/api/pipeline/cancel`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`No se pudo cancelar (${response.status}): ${text}`);
  }
  return response.json();
}

/**
 * System version (installed locally).
 */
export interface SystemVersion {
  version: string;
  gestoria_nif: string;
  gestoria_nombre: string;
}

export async function fetchSystemVersion(): Promise<SystemVersion> {
  const response = await fetchWithTimeout(`${API_URL}/api/system/version`);
  if (!response.ok) throw new Error("No se pudo leer la versión instalada");
  return response.json();
}

/**
 * Latest published version. Returns update_available=true when newer than installed.
 */
export interface LatestVersion {
  version: string | null;
  current: string;
  update_available: boolean;
  changelog?: string;
  released_at?: string;
  error?: string;
}

export async function fetchLatestVersion(opts?: { force?: boolean }): Promise<LatestVersion> {
  const url = opts?.force
    ? `${API_URL}/api/system/latest-version?force=true`
    : `${API_URL}/api/system/latest-version`;
  const response = await fetchWithTimeout(url);
  if (!response.ok) throw new Error("No se pudo comprobar actualizaciones");
  return response.json();
}

/**
 * Solicita la instalación de la última versión publicada.
 * El backend escribe un flag que recoge una tarea programada de Windows
 * que ejecuta `docker compose pull && up -d`.
 */
export interface UpdateRequestResponse {
  status: "requested";
  eta_seconds: number;
  message: string;
}

export type UpdateErrorCode =
  | "watchtower_unreachable"
  | "auth_failed"
  | "pipeline_busy"
  | "watchtower_error"
  | "not_configured"
  | "unknown";

export class UpdateError extends Error {
  code: UpdateErrorCode;
  httpStatus: number;
  watchtowerStatus?: number;
  watchtowerBody?: string;

  constructor(opts: {
    code: UpdateErrorCode;
    message: string;
    httpStatus: number;
    watchtowerStatus?: number;
    watchtowerBody?: string;
  }) {
    super(opts.message);
    this.name = "UpdateError";
    this.code = opts.code;
    this.httpStatus = opts.httpStatus;
    this.watchtowerStatus = opts.watchtowerStatus;
    this.watchtowerBody = opts.watchtowerBody;
  }
}

export async function requestSystemUpdate(): Promise<UpdateRequestResponse> {
  const response = await fetchWithTimeout(`${API_URL}/api/system/update`, {
    method: "POST",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    const detail = (data as { detail?: unknown }).detail;
    // El backend devuelve detail como objeto discriminado {code, message, ...} desde 0.2.6.
    // Para compatibilidad con backends antiguos, también aceptamos detail string.
    if (detail && typeof detail === "object" && "code" in detail) {
      const d = detail as {
        code: UpdateErrorCode;
        message?: string;
        watchtower_status?: number;
        watchtower_body?: string;
      };
      throw new UpdateError({
        code: d.code,
        message: d.message || "Error en la actualización",
        httpStatus: response.status,
        watchtowerStatus: d.watchtower_status,
        watchtowerBody: d.watchtower_body,
      });
    }
    throw new UpdateError({
      code: response.status === 409 ? "pipeline_busy" : "unknown",
      message:
        typeof detail === "string"
          ? detail
          : `No se pudo solicitar la actualización (${response.status})`,
      httpStatus: response.status,
    });
  }
  return response.json();
}

/**
 * Submit feedback (Problema/Recomendación/Pregunta) — backend forwards to Discord.
 */
export type FeedbackTipo = "problema" | "recomendacion" | "pregunta";

export interface FeedbackPayload {
  tipo: FeedbackTipo;
  descripcion: string;
  incluir_logs: boolean;
  navegador?: string;
}

export async function submitFeedback(payload: FeedbackPayload): Promise<{
  status: string;
  delivered: boolean;
  delivery_error?: string | null;
}> {
  const response = await fetchWithTimeout(`${API_URL}/api/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Error enviando feedback (${response.status}): ${text}`);
  }
  return response.json();
}
