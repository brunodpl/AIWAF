import type { ApprovedInvoiceData, FiscalLine, Client } from "./types";

/**
 * Normalize a string for use as a concept in CSV.
 * Lowercase, remove accents, max 50 chars.
 */
function normalizeConcept(concept: string): string {
  return concept
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9\s_-]/g, "")
    .slice(0, 50);
}

/**
 * Escape a CSV field value. If it contains commas, quotes, or newlines,
 * wrap in double quotes and escape internal quotes.
 */
function csvEscape(value: string | number): string {
  const str = String(value);
  if (str.includes(",") || str.includes('"') || str.includes("\n") || str.includes("\r")) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

/**
 * Format a date from YYYY-MM-DD to DD/MM/YYYY.
 */
function formatDateDDMMYYYY(dateStr: string): string {
  if (!dateStr) return "";
  // Handle ISO8601 or YYYY-MM-DD
  const parts = dateStr.split("T")[0].split("-");
  if (parts.length === 3) {
    return `${parts[2]}/${parts[1]}/${parts[0]}`;
  }
  return dateStr;
}

interface CSVRow {
  idAsiento: string;
  fechaExpedicion: string;
  numFactura: string;
  nifEmisor: string;
  nombreEmisor: string;
  cuentaContable: string;
  conceptoGasto: string;
  baseImponible: string;
  tipoIVA: string;
  cuotaIVA: string;
  totalFactura: string;
  claseFiscal: string;
}

/**
 * Generate CSV content from approved invoices.
 *
 * - UTF-8 with BOM (\uFEFF)
 * - One row per fiscal line
 * - Sequential AS-500, AS-501... IDs
 * - CRLF line endings
 *
 * @param approvedInvoices Map of docId -> ApprovedInvoiceData
 * @param clientsLookup Map of NIF -> Client (for account lookup)
 * @returns CSV string ready for Blob creation
 */
export function generateCSV(
  approvedInvoices: Map<string, ApprovedInvoiceData>,
  clientsLookup: Map<string, Client>
): string {
  const HEADER =
    "ID_Asiento,Fecha_Expedicion,Num_Factura,NIF_Emisor,Nombre_Emisor,Cuenta_Contable,Concepto_Gasto,Base_Imponible,Tipo_IVA,Cuota_IVA,Total_Factura,Clase_Fiscal";

  const rows: CSVRow[] = [];
  let asientoIndex = 500;

  // Sort invoices deterministically: by fecha_expedicion, then numero_factura
  const sortedInvoices = Array.from(approvedInvoices.entries()).sort((a, b) => {
    const dateA = a[1].formData.fecha_expedicion || "";
    const dateB = b[1].formData.fecha_expedicion || "";
    if (dateA !== dateB) return dateA.localeCompare(dateB);
    const numA = a[1].formData.numero_factura || "";
    const numB = b[1].formData.numero_factura || "";
    return numA.localeCompare(numB);
  });

  for (const [_docId, invoice] of sortedInvoices) {
    const idAsiento = `AS-${asientoIndex}`;
    asientoIndex++;

    const formData = invoice.formData;
    const fechaExpedicion = formatDateDDMMYYYY(formData.fecha_expedicion || "");
    const numFactura = formData.numero_factura || "";
    const nifEmisor = formData.nif_entidad || "";
    const nombreEmisor = formData.nombre_entidad || "";
    const totalFactura = formData.total_euros || "0";
    const conceptoRaw = formData.concepto || "";
    const conceptoGasto = normalizeConcept(conceptoRaw);
    const claseFiscal = invoice.clase_fiscal || "gasto_deducible_interior";

    // Account: priority order:
    // 1. invoice.cuenta_contable (top-level, set by approve flow)
    // 2. formData.cuenta_contable (field edited by user in review stage)
    // 3. clientsLookup by NIF
    let cuentaContable = invoice.cuenta_contable || formData.cuenta_contable || "";
    if (!cuentaContable) {
      const nifReceptor = formData.nif_receptor || "";
      if (nifReceptor && clientsLookup.has(nifReceptor.toUpperCase())) {
        cuentaContable = clientsLookup.get(nifReceptor.toUpperCase())!.cuenta_contable;
      } else if (nifEmisor && clientsLookup.has(nifEmisor.toUpperCase())) {
        cuentaContable = clientsLookup.get(nifEmisor.toUpperCase())!.cuenta_contable;
      }
    }

    // Generate one row per fiscal line
    for (const line of invoice.fiscalLines) {
      const vatRate =
        line.vatRate !== null && line.vatRate !== undefined ? String(line.vatRate) : "";

      rows.push({
        idAsiento,
        fechaExpedicion,
        numFactura,
        nifEmisor,
        nombreEmisor,
        cuentaContable,
        conceptoGasto,
        baseImponible: Number(line.base).toFixed(2),
        tipoIVA: vatRate,
        cuotaIVA: Number(line.vatAmount).toFixed(2),
        totalFactura: Number(totalFactura).toFixed(2),
        claseFiscal,
      });
    }
  }

  // Build CSV
  const lines = [HEADER];
  for (const row of rows) {
    lines.push(
      [
        csvEscape(row.idAsiento),
        csvEscape(row.fechaExpedicion),
        csvEscape(row.numFactura),
        csvEscape(row.nifEmisor),
        csvEscape(row.nombreEmisor),
        csvEscape(row.cuentaContable),
        csvEscape(row.conceptoGasto),
        csvEscape(row.baseImponible),
        csvEscape(row.tipoIVA),
        csvEscape(row.cuotaIVA),
        csvEscape(row.totalFactura),
        csvEscape(row.claseFiscal),
      ].join(",")
    );
  }

  // UTF-8 BOM + CRLF
  return "\uFEFF" + lines.join("\r\n") + "\r\n";
}

const INTERMEGA_HEADER =
  "FECHA,SERIE,Nº FACTURA,NOMBRE CLI-PRO,NIF CLI-PRO,DESCRIPCION," +
  "BASE,%IVA,CUOTA IVA,%RECARGO EQUIVALENCIA,CUOTA RECARGO EQUIVALENCIA," +
  "%RETENCION,IMPORTE RETENCION,BASE EXENTA,TOTAL FACTURA";

function intermegaRowsFor(invoice: ApprovedInvoiceData): string[] {
  const f = invoice.formData;
  const fecha = formatDateDDMMYYYY(f.fecha_expedicion || "");
  const total = Number(f.total_euros || 0).toFixed(2);
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
      Number(line.base).toFixed(2),
      pctIVA,
      Number(line.vatAmount).toFixed(2),
      "", "",                                  // %RE / Cuota RE
      "0", "0",                               // %Ret / Importe Ret
      "",                                      // BASE EXENTA
      total,
    ];
    return fields.map((v) => csvEscape(String(v))).join(",");
  });
}

/**
 * Generate Intermega FISC CSV content, partitioned by clase_fiscal.
 * Returns two CSV strings: emitidas (ingreso_*) and recibidas (gasto_*).
 */
export function generateIntermegaCSV(
  approvedInvoices: Map<string, ApprovedInvoiceData>
): { emitidas: string; recibidas: string } {
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

  for (const [, invoice] of sorted) {
    const rows = intermegaRowsFor(invoice);
    const claseFiscal = invoice.clase_fiscal || "gasto_deducible_interior";
    const target = claseFiscal.startsWith("ingreso") ? emitidas : recibidas;
    target.push(...rows);
  }

  const bom = "\uFEFF";
  return {
    emitidas: bom + emitidas.join("\r\n") + "\r\n",
    recibidas: bom + recibidas.join("\r\n") + "\r\n",
  };
}

/**
 * Download the two Intermega CSV files (emitidas + recibidas) in the browser.
 */
export function downloadIntermegaCSV(
  emitidas: string,
  recibidas: string
): void {
  const dateTag = new Date()
    .toISOString()
    .slice(0, 10)
    .replace(/-/g, "");
  const files = [
    { content: emitidas, name: `facturas_emitidas_${dateTag}.csv` },
    { content: recibidas, name: `facturas_recibidas_${dateTag}.csv` },
  ];
  for (const { content, name } of files) {
    const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }
}

/**
 * Download the CSV file in the browser.
 */
export function downloadCSV(csvContent: string): void {
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;

  const now = new Date();
  const timestamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}_${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
  link.download = `asientos_${timestamp}.csv`;

  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
