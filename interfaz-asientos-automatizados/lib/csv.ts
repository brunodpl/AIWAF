import type { ApprovedInvoiceData } from "./types";
import { resolveClienteGestoria, resolveContraparteFactura } from "./cliente-gestoria";

/**
 * Escape a CSV field for semicolon-delimited format (Spanish Excel locale).
 * Wraps in double quotes if the value contains semicolons, quotes or newlines.
 */
function csvEscape(value: string | number): string {
  const str = String(value);
  if (str.includes(";") || str.includes('"') || str.includes("\n") || str.includes("\r")) {
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
  "FECHA;SERIE;Nº FACTURA;NOMBRE CLI-PRO;NIF CLI-PRO;DESCRIPCION;" +
  "BASE;%IVA;CUOTA IVA;%RECARGO EQUIVALENCIA;CUOTA RECARGO EQUIVALENCIA;" +
  "%RETENCION;IMPORTE RETENCION;BASE EXENTA;TOTAL FACTURA";

function toSpanishDecimal(n: number): string {
  return n.toFixed(2).replace(".", ",");
}

/** Strip Excel text-prefix artifacts and invalid chars from NIF/CIF. */
function sanitizeNIF(raw: string): string {
  return raw
    .trim()
    .replace(/^[^A-Za-z0-9]+/, "") // leading non-alphanumeric (´, ', `, etc.)
    .replace(/\s+/g, "")           // internal spaces
    .toUpperCase();
}

export function intermegaRowsFor(invoice: ApprovedInvoiceData): string[] {
  const f = invoice.formData;
  const fecha = formatDateDDMMYYYY(f.fecha_expedicion || "");
  const total = toSpanishDecimal(normalizeNumber(f.total_euros));
  // CLI-PRO is ALWAYS the counterpart of our gestoria's client.
  const contraparte = resolveContraparteFactura(invoice.libro, f);
  const nombreCliPro = contraparte.nombre;
  const nifCliPro = sanitizeNIF(contraparte.nif);
  const concepto = f.concepto || "";

  return invoice.fiscalLines.map((line) => {
    // vatRate 0 → exenta → Intermega usa tipo 9
    const pctIVA = line.vatRate === 0 ? "9" : String(line.vatRate ?? "");
    const fields = [
      fecha,
      "",                                      // SERIE
      f.numero_factura || "",
      nombreCliPro,
      nifCliPro,
      concepto,
      toSpanishDecimal(normalizeNumber(line.base)),
      pctIVA,
      toSpanishDecimal(normalizeNumber(line.vatAmount)),
      "", "",                                  // %RE / Cuota RE
      "0", "0",                               // %Ret / Importe Ret
      "",                                      // BASE EXENTA
      total,
    ];
    return fields.map((v) => csvEscape(String(v))).join(";");
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

export interface IntermegaCsvFile {
  filename: string;
  content: string;
  nifCliente: string;        // sanitized; "SIN_CLIENTE" if unresolved
  nombreCliente: string;
  tipo: "emitidas" | "recibidas";
  rowCount: number;          // number of invoices, not fiscal lines
}

function dateTag(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}${m}${d}`;
}

function sanitizeNifForFilename(nif: string): string {
  const clean = (nif || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  return clean || "SIN_CLIENTE";
}

/**
 * Build Intermega CSVs partitioned by (gestoría client NIF × tipo).
 *
 * - Tipo derived from libro: "ingresos" → emitidas; otherwise → recibidas.
 * - Cliente NIF derived in real time via `resolveClienteGestoria` (NOT from
 *   the stale `formData.nif_cliente` snapshot).
 * - Invoices with no resolvable NIF are grouped under "SIN_CLIENTE".
 *
 * Filename pattern: `{YYYYMMDD}_{NIF}_{NN}_{tipo}.csv`. Sequence (`NN`) is
 * batch-local, ordered by NIF alphabetically with emitidas before recibidas
 * for the same NIF.
 */
export function generateIntermegaCSVsByCliente(
  approvedInvoices: Map<string, ApprovedInvoiceData>,
  date: Date = new Date()
): IntermegaCsvFile[] {
  type Group = {
    nifSan: string;                    // sanitized for filename
    nombre: string;
    tipo: "emitidas" | "recibidas";
    invoices: ApprovedInvoiceData[];
  };
  const groups = new Map<string, Group>();

  for (const inv of approvedInvoices.values()) {
    const cliente = resolveClienteGestoria(inv.libro, inv.formData);
    const nifSan = sanitizeNifForFilename(cliente.nif);
    const tipo: "emitidas" | "recibidas" = inv.libro === "ingresos" ? "emitidas" : "recibidas";
    const key = `${nifSan}__${tipo}`;
    let g = groups.get(key);
    if (!g) {
      g = { nifSan, nombre: cliente.nombre, tipo, invoices: [] };
      groups.set(key, g);
    } else if (!g.nombre && cliente.nombre) {
      // first non-empty name wins
      g.nombre = cliente.nombre;
    }
    g.invoices.push(inv);
  }

  // Stable sort: by NIF alpha, then emitidas < recibidas
  const sorted = [...groups.values()].sort((a, b) => {
    if (a.nifSan !== b.nifSan) return a.nifSan < b.nifSan ? -1 : 1;
    return a.tipo === b.tipo ? 0 : (a.tipo === "emitidas" ? -1 : 1);
  });

  const tag = dateTag(date);
  const bom = "\uFEFF";

  return sorted.map((g, idx) => {
    const seq = String(idx + 1).padStart(2, "0");
    const filename = `${tag}_${g.nifSan}_${seq}_${g.tipo}.csv`;
    // Sort invoices within the group by date then numero_factura for stability
    const invs = [...g.invoices].sort((a, b) => {
      const da = a.formData.fecha_expedicion || "";
      const db = b.formData.fecha_expedicion || "";
      if (da !== db) return da.localeCompare(db);
      return (a.formData.numero_factura || "").localeCompare(b.formData.numero_factura || "");
    });
    const lines: string[] = [INTERMEGA_HEADER];
    for (const i of invs) lines.push(...intermegaRowsFor(i));
    const content = bom + lines.join("\r\n") + "\r\n";
    return {
      filename,
      content,
      nifCliente: g.nifSan,
      nombreCliente: g.nombre,
      tipo: g.tipo,
      rowCount: g.invoices.length,
    };
  });
}
