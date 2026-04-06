export type FieldStatus = "auto" | "warn" | "block";

export interface InvoiceField {
  id: string;
  label: string;
  value: string;
  status: FieldStatus;
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
  fields: InvoiceField[];
  fiscalLines: FiscalLine[];
}
