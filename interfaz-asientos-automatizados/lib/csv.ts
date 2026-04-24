import type { ApprovedInvoiceData } from "./types";

/**
 * Escape a CSV field. If it contains commas, quotes or newlines, wrap in
 * double quotes and escape internal quotes.
 */
function csvEscape(value: string | number): string {
  const str = String(value);
  if (str.includes(",") || str.includes('"') || str.includes("\n") || str.includes("\r")) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

/** Format YYYY-MM-DD (or ISO8601) → DD/MM/YYYY. */
function formatDateDDMMYYYY(dateStr: string): string {
  if (!dateStr) return "";
  const parts = dateStr.split("T")[0].split("-");
  if (parts.length === 3) return `${parts[2]}/${parts[1]}/${parts[0]}`;
  return dateStr;
}

/**
 * Parse a possibly-Spanish-formatted numeric string ("131,00", "1.234,56")
 * into a plain Number. Empty / invalid → 0.
 */
export function normalizeNumber(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === "") return 0;
  if (typeof value === "number") return isFinite(value) ? value : 0;
  // Spanish format detection: has comma decimal and dot thousands
  const hasComma = value.includes(",");
  const hasDot = value.includes(".");
  let normalized = value.trim();
  if (hasComma && hasDot) {
    // "1.234,56" → "1234.56"
    normalized = normalized.replace(/\./g, "").replace(",", ".");
  } else if (hasComma) {
    // "131,00" → "131.00"
    normalized = normalized.replace(",", ".");
  }
  const n = Number(normalized);
  return isFinite(n) ? n : 0;
}

const INTERMEGA_HEADER =
  "FECHA,SERIE,Nº FACTURA,NOMBRE CLI-PRO,NIF CLI-PRO,DESCRIPCION," +
  "BASE,%IVA,CUOTA IVA,%RECARGO EQUIVALENCIA,CUOTA RECARGO EQUIVALENCIA," +
  "%RETENCION,IMPORTE RETENCION,BASE EXENTA,TOTAL FACTURA";

function intermegaRowsFor(invoice: ApprovedInvoiceData): string[] {
  const f = invoice.formData;
  const fecha = formatDateDDMMYYYY(f.fecha_expedicion || "");
  const total = normalizeNumber(f.total_euros).toFixed(2);
  const nombreCliente = f.nombre_cliente || f.nombre_receptor || "";
  const nifCliente = f.nif_cliente || f.nif_receptor || "";
  const concepto = f.concepto || "";

  return invoice.fiscalLines.map((line) => {
    // vatRate 0 → exenta → Intermega usa tipo 9
    const pctIVA = line.vatRate === 0 ? "9" : String(line.vatRate ?? "");
    const fields = [
      fecha,
      "",                                      // SERIE
      f.numero_factura || "",
      nombreCliente,
      nifCliente,
      concepto,
      normalizeNumber(line.base).toFixed(2),
      pctIVA,
      normalizeNumber(line.vatAmount).toFixed(2),
      "", "",                                  // %RE / Cuota RE
      "0", "0",                               // %Ret / Importe Ret
      "",                                      // BASE EXENTA
      total,
    ];
    return fields.map((v) => csvEscape(String(v))).join(",");
  });
}

/**
 * Generate Intermega FISC CSVs partitioned by libro.
 * - libro === "ingresos"  → emitidas
 * - libro === "gastos" | "bienes" | undefined → recibidas
 * Each CSV is UTF-8 with BOM, CRLF, and literal Intermega header.
 */
export function generateIntermegaCSV(
  approvedInvoices: Map<string, ApprovedInvoiceData>
): { emitidas: string; recibidas: string; counts: { emitidas: number; recibidas: number } } {
  const sorted = Array.from(approvedInvoices.entries()).sort((a, b) => {
    const dateA = a[1].formData.fecha_expedicion || "";
    const dateB = b[1].formData.fecha_expedicion || "";
    if (dateA !== dateB) return dateA.localeCompare(dateB);
    return (a[1].formData.numero_factura || "").localeCompare(
      b[1].formData.numero_factura || ""
    );
  });

  const emitidas: string[] = [INTERMEGA_HEADER];
  const recibidas: string[] = [INTERMEGA_HEADER];
  let nEmitidas = 0;
  let nRecibidas = 0;

  for (const [, invoice] of sorted) {
    const rows = intermegaRowsFor(invoice);
    if (invoice.libro === "ingresos") {
      emitidas.push(...rows);
      nEmitidas++;
    } else {
      recibidas.push(...rows);
      nRecibidas++;
    }
  }

  const bom = "\uFEFF";
  return {
    emitidas: bom + emitidas.join("\r\n") + "\r\n",
    recibidas: bom + recibidas.join("\r\n") + "\r\n",
    counts: { emitidas: nEmitidas, recibidas: nRecibidas },
  };
}

function downloadBlob(content: string, filename: string): void {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function todayTag(): string {
  return new Date().toISOString().slice(0, 10).replace(/-/g, "");
}

export function downloadIntermegaEmitidas(csv: string): void {
  downloadBlob(csv, `facturas_emitidas_${todayTag()}.csv`);
}

export function downloadIntermegaRecibidas(csv: string): void {
  downloadBlob(csv, `facturas_recibidas_${todayTag()}.csv`);
}
