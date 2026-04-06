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

export interface InvoiceDocument {
  id: string;
  status: "pending" | "approved" | "rejected";
  imageUrl: string;
  fileType: "pdf" | "image";
  decision_global?: "auto" | "warn" | "block" | "pendiente";
  fields: InvoiceField[];
  fiscalLines: FiscalLine[];
}

/** A file pending in a PENDIENTES book folder */
export interface BookFile {
  name: string;
  size_kb: number;
  added: string; // ISO8601
}

/** An accounting book (gastos, ingresos, bienes) */
export interface Book {
  id: "gastos" | "ingresos" | "bienes";
  label: string;
  folder: string;
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
  clase_fiscal: string;
  cuenta_contable: string;
}
