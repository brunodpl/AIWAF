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
  | "processing"
  | "review"
  | "done"
  | "blocked"
  | "error"
  | "renamed"
  | "reset";

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
