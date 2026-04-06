/**
 * API client for communicating with the pipeline backend.
 */

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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

export interface FiscalLine {
  id: string;
  base: number;
  tipo_iva: number;
  cuota: number;
  total: number;
}

export interface InvoiceField {
  id: string;
  label: string;
  value: string;
  status: "auto" | "warn" | "block";
  confidence: number;
  reason?: string;
  group: number;
  editable?: boolean;
}

export interface InvoiceDocument {
  id: string;
  status: "pending" | "approved" | "rejected";
  imageUrl?: string;
  fields: InvoiceField[];
  fiscalLines: FiscalLine[];
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
  fiscal_lines: FiscalLine[];
  artifacts: string[];
  file_url?: string;
  invoice_filename?: string;
}

/**
 * Fetch the list of processed invoices.
 */
export async function fetchInvoices(): Promise<InvoiceListResponse> {
  const response = await fetch(`${API_URL}/api/invoices`);
  
  if (!response.ok) {
    throw new Error(`Failed to fetch invoices: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Fetch detailed information for a specific invoice.
 */
export async function fetchInvoiceDetail(docId: string): Promise<InvoiceDetailResponse> {
  const response = await fetch(`${API_URL}/api/invoices/${docId}`);
  
  if (!response.ok) {
    if (response.status === 404) {
      throw new Error(`Invoice ${docId} not found`);
    }
    throw new Error(`Failed to fetch invoice: ${response.statusText}`);
  }
  
  return response.json();
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

  const fields: InvoiceField[] = Object.entries(detail.fields)
    .filter(([_, value]) => value && value.valor_final !== undefined)
    .map(([key, value]) => {
      const mapping = fieldMapping[key] || { label: key, group: 99 };
      return {
        id: key,
        label: mapping.label,
        value: String(value.valor_final || ""),
        status: value.decision as "auto" | "warn" | "block",
        confidence: Math.round((value.confianza || 0) * 100),
        reason: value.motivo,
        group: mapping.group,
        editable: value.decision !== "auto",
      };
    });

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
  notes?: string
): Promise<any> {
  const response = await fetch(`${API_URL}/api/invoices/${docId}/action`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      action,
      document_id: docId,
      notes: notes || undefined,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to send action: ${response.statusText}`);
  }

  return response.json();
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
  const response = await fetch(`${API_URL}/api/stats`);
  
  if (!response.ok) {
    throw new Error(`Failed to fetch stats: ${response.statusText}`);
  }
  
  return response.json();
}
