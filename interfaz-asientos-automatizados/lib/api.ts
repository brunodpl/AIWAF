/**
 * API client for communicating with the pipeline backend.
 */

import type { InvoiceDocument, FieldDecision, Book, PipelineStatus, Client } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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

export interface InvoiceListResponse {
  invoices: Array<{
    id: string;
    decision_global: string;
    timestamp: string;
    nif_entidad: string;
    nombre_entidad: string;
    numero_factura: string;
    total_euros: number;
  }>;
  total: number;
}

export interface InvoiceDetailResponse {
  id: string;
  decision_global: string;
  metadata: any;
  fields: Record<string, FieldStatus>;
  fiscal_lines: ApiFiscalLine[];
  artifacts: string[];
  file_url?: string;
  invoice_filename?: string;
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

  return {
    id: detail.id,
    status: detail.decision_global === "auto" ? "approved" : "pending",
    imageUrl: imageUrl || fileUrl,
    fileType,
    decision_global: detail.decision_global as InvoiceDocument["decision_global"],
    fields,
    fiscalLines,
  };
}

/**
 * Send an action (approve/reject) for an invoice.
 */
export async function sendInvoiceAction(
  docId: string,
  action: "approve" | "reject",
  corrections?: {
    fields?: Record<string, string>;
    fiscalLines?: Array<{ base: number; tipo_iva: number | null; cuota: number; total: number }>;
  }
): Promise<{ status: string; message: string; action: string }> {
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
      const error = await response.text();
      throw new Error(`Failed to upload files: ${error}`);
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
      throw new Error(`Failed to run pipeline: ${errorText}`);
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
