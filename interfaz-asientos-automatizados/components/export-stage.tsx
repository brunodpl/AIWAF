"use client";

import { useState, useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Download,
  ArrowLeft,
  Loader2,
  AlertTriangle,
} from "lucide-react";
import { ApprovedInvoiceData } from "@/lib/types";
import {
  generateIntermegaCSVsByCliente,
  type IntermegaCsvFile,
  normalizeNumber,
} from "@/lib/csv";
import { resolveClienteGestoria, resolveContraparteFactura } from "@/lib/cliente-gestoria";
import { buildCsvBase64, confirmBatch } from "@/lib/api-clients";
import { LIBRO_FRONT_TO_SHORT } from "@/lib/api";
import type {
  AsientoConfirm,
  ConfirmBatchPayload,
  ConfirmBatchResponse,
  LineaAsiento,
} from "@/lib/types";
import JSZip from "jszip";

type ExportKind = "emitidas" | "recibidas";

interface ExportStageProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  onBack: () => void;
  /**
   * Disparado tras un POST /api/pipeline/confirm con éxito.
   * El padre limpia approvedInvoices + localStorage y vuelve a "books".
   */
  onConfirmed?: (response: {
    facturas_confirmadas: number;
    clientes_nuevos: number;
  }) => void;
}

interface AsientoRow {
  docId: string;
  nif_cliente: string;
  nombre_cliente: string;
  fecha_operacion: string;
  fecha_expedicion: string;
  numero_factura: string;
  cuenta_contable: string;
  nombre_contraparte: string;
  nif_contraparte: string;
  concepto: string;
  base_euros: string;
  tipo_porcentaje: string;
  cuota: string;
  total_euros: string;
  kind: ExportKind;
}

// Pending action is either a single-file download or "zip"
type PendingAction = { type: "file"; file: IntermegaCsvFile } | { type: "zip" };

function invoiceKind(invoice: ApprovedInvoiceData): ExportKind {
  return invoice.libro === "ingresos" ? "emitidas" : "recibidas";
}

function sanitizeNifKey(nif: string): string {
  return (nif || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase() || "SIN_CLIENTE";
}

/** FECHA y Nº FACTURA son obligatorios en Intermega: un fichero con filas que
 *  los omitan se rechaza entero ("no se han encontrado registros" / error de
 *  importación). Avisamos al operario antes de descargar. */
function isMissingRequiredFields(fd: Record<string, string>): boolean {
  return !fd.fecha_expedicion || !fd.numero_factura;
}

function buildAsientoRows(approvedInvoices: Map<string, ApprovedInvoiceData>): AsientoRow[] {
  const rows: AsientoRow[] = [];

  for (const [docId, invoice] of approvedInvoices) {
    const fd = invoice.formData;
    const cliente = resolveClienteGestoria(invoice.libro, fd);
    const contraparte = resolveContraparteFactura(invoice.libro, fd);
    const nifCliente = sanitizeNifKey(cliente.nif);
    const nombreCliente = cliente.nombre || "Cliente desconocido";
    const totalFactura = normalizeNumber(fd.total_euros).toFixed(2);
    const kind = invoiceKind(invoice);

    const base = {
      docId,
      nif_cliente: nifCliente,
      nombre_cliente: nombreCliente,
      fecha_operacion: fd.fecha_operacion || "",
      fecha_expedicion: fd.fecha_expedicion || "",
      numero_factura: fd.numero_factura || "",
      cuenta_contable: fd.cuenta_contable || "",
      nombre_contraparte: contraparte.nombre || "",
      nif_contraparte: contraparte.nif || "",
      concepto: fd.concepto || "",
      kind,
    };

    if (invoice.fiscalLines.length > 0) {
      for (const line of invoice.fiscalLines) {
        rows.push({
          ...base,
          base_euros: normalizeNumber(line.base).toFixed(2),
          tipo_porcentaje: line.vatRate != null && line.vatRate !== 0 ? String(line.vatRate) : "EXENTA",
          cuota: normalizeNumber(line.vatAmount).toFixed(2),
          total_euros: totalFactura,
        });
      }
    } else {
      rows.push({
        ...base,
        base_euros: totalFactura,
        tipo_porcentaje: "",
        cuota: "0.00",
        total_euros: totalFactura,
      });
    }
  }

  return rows;
}

function groupByCliente(rows: AsientoRow[]): Map<string, { nombre: string; rows: AsientoRow[] }> {
  const groups = new Map<string, { nombre: string; rows: AsientoRow[] }>();
  for (const row of rows) {
    const key = row.nif_cliente;
    if (!groups.has(key)) {
      groups.set(key, { nombre: row.nombre_cliente, rows: [] });
    }
    groups.get(key)!.rows.push(row);
  }
  return groups;
}

function downloadCsvFile(file: IntermegaCsvFile) {
  const blob = new Blob([file.content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = file.filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Construye el payload de confirm batch a partir del Map de aprobadas y
 * envía POST /api/pipeline/confirm. Devuelve la respuesta o null si no hay
 * facturas que confirmar.
 *
 * Persiste por cada doc_id:
 *   - libros/asientos/{folder}/resultado_final.json (diff con validación)
 *   - libros/asientos/{folder}/asiento_{doc_id}.csv (CSV físico para Hacienda)
 *   - .state.json → último evento "done"
 *   - maestro_clientes.yaml actualizado bajo file-lock
 */
async function persistBatchToBackend(
  approvedInvoices: Map<string, ApprovedInvoiceData>,
): Promise<ConfirmBatchResponse | null> {
  if (approvedInvoices.size === 0) return null;

  const doc_ids: string[] = [];
  const asientos: Record<string, AsientoConfirm> = {};

  for (const [docId, invoice] of approvedInvoices) {
    const fd = invoice.formData;
    const cliente = resolveClienteGestoria(invoice.libro, fd);

    // campos_finales — todo lo que el operario dejó en el form, tal cual.
    // El backend hace el diff frente a resultado_validacion.json y marca
    // qué campos llevan editado:true.
    const camposFinales: Record<string, { valor: string | number | null }> = {};
    for (const [k, v] of Object.entries(fd)) {
      camposFinales[k] = { valor: v ?? null };
    }
    // Asegurar campos clave para la tarjeta de cliente del grid.
    camposFinales["nif_cliente"] = { valor: cliente.nif || null };
    camposFinales["nombre_cliente"] = { valor: cliente.nombre || null };
    camposFinales["cuenta_contable"] = { valor: (fd.cuenta_contable || "").toUpperCase() || null };
    camposFinales["concepto"] = { valor: (fd.concepto || "").toUpperCase() || null };

    const lineas: LineaAsiento[] = invoice.fiscalLines.map((l) => {
      const base = normalizeNumber(l.base);
      const cuota = normalizeNumber(l.vatAmount);
      return {
        cuenta: (fd.cuenta_contable || "").toUpperCase(),
        concepto: (fd.concepto || "").toUpperCase(),
        debe: base + cuota,
        haber: 0,
        tipo_iva: l.vatRate ?? 0,
        base_imponible: base,
      };
    });

    asientos[docId] = {
      campos_finales: camposFinales,
      lineas_asiento: lineas,
      csv_b64: buildCsvBase64(lineas),
      // Override de libro: si el operario cambió la cuenta contable durante
      // la revisión, `invoice.libro` puede diferir del libro inicial del
      // splitter. El backend prefiere este valor sobre `_libro_from_folder`.
      libro: invoice.libro ? LIBRO_FRONT_TO_SHORT[invoice.libro] : undefined,
    };
    doc_ids.push(docId);
  }

  const payload: ConfirmBatchPayload = { doc_ids, asientos };
  return await confirmBatch(payload);
}


async function downloadZip(files: IntermegaCsvFile[]): Promise<void> {
  const zip = new JSZip();
  for (const f of files) zip.file(f.filename, f.content);
  const blob = await zip.generateAsync({ type: "blob" });
  const today = new Date();
  const tag = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, "0")}${String(today.getDate()).padStart(2, "0")}`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `intermega_${tag}.zip`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function ExportStage({ approvedInvoices, onBack, onConfirmed }: ExportStageProps) {
  const [isZipping, setIsZipping] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [missingAccountsCount, setMissingAccountsCount] = useState(0);
  const [missingFilesCount, setMissingFilesCount] = useState(0);
  const [missingRequiredCount, setMissingRequiredCount] = useState(0);
  const [missingRequiredFilesCount, setMissingRequiredFilesCount] = useState(0);
  const [missingFiscalCount, setMissingFiscalCount] = useState(0);
  const [missingFiscalFilesCount, setMissingFiscalFilesCount] = useState(0);

  /**
   * "CONFIRMAR Y SEGUIR ESCANEANDO" — CTA primario del flujo:
   * llama POST /api/pipeline/confirm que persiste, por cada doc_id:
   *   - resultado_final.json con diff editado:true/false
   *   - asiento_{doc_id}.csv físico + hash SHA-256
   *   - maestro_clientes.yaml actualizado (lock cross-process)
   *   - .state.json → último evento "done"
   *
   * Bloquea UI con flag isConfirming para impedir doble click. Tras éxito,
   * el padre limpia approvedInvoices/localStorage y vuelve a "books".
   */
  const handleConfirmAndContinue = useCallback(async () => {
    if (isConfirming || approvedInvoices.size === 0) return;
    setIsConfirming(true);
    try {
      const res = await persistBatchToBackend(approvedInvoices);
      if (!res) {
        toast.error("No hay facturas para confirmar");
        return;
      }
      const baseMsg =
        `${res.facturas_confirmadas} factura${res.facturas_confirmadas === 1 ? "" : "s"} confirmada${res.facturas_confirmadas === 1 ? "" : "s"}` +
        (res.clientes_nuevos > 0
          ? ` · ${res.clientes_nuevos} cliente${res.clientes_nuevos === 1 ? "" : "s"} nuevo${res.clientes_nuevos === 1 ? "" : "s"}`
          : "") +
        (res.inbox_pdfs_deleted && res.inbox_pdfs_deleted > 0
          ? ` · ${res.inbox_pdfs_deleted} PDF${res.inbox_pdfs_deleted === 1 ? "" : "s"} archivad${res.inbox_pdfs_deleted === 1 ? "o" : "os"}`
          : "");

      if (res.ok) {
        toast.success(baseMsg);
      } else {
        // Errores parciales: mostrar warning con detalle por doc.
        const errCount = res.errors?.length ?? 0;
        toast.warning(
          `${baseMsg} · ${errCount} con error parcial — ver detalle abajo`,
        );
        if (res.errors) {
          res.errors.forEach((e) =>
            toast.error(`${e.doc_id}: ${e.error} (fase: ${e.stage})`, { duration: 8000 }),
          );
        }
      }
      onConfirmed?.({
        facturas_confirmadas: res.facturas_confirmadas,
        clientes_nuevos: res.clientes_nuevos,
      });
    } catch (err) {
      toast.error("Error confirmando el lote. Revisa la conexión y reintenta.");
    } finally {
      setIsConfirming(false);
    }
  }, [approvedInvoices, isConfirming, onConfirmed]);

  // CSV files grouped by (client × tipo), computed once per render cycle
  const csvFiles = useMemo(
    () => generateIntermegaCSVsByCliente(approvedInvoices),
    [approvedInvoices]
  );

  const hasSinCliente = useMemo(
    () => csvFiles.some((f) => f.nifCliente === "SIN_CLIENTE"),
    [csvFiles]
  );

  // Preview table rows + grouping (uses resolveClienteGestoria via buildAsientoRows)
  const allRows = useMemo(() => buildAsientoRows(approvedInvoices), [approvedInvoices]);
  const grouped = useMemo(() => groupByCliente(allRows), [allRows]);

  const { countEmitidas, countRecibidas } = useMemo(() => {
    let e = 0, r = 0;
    for (const [, invoice] of approvedInvoices) {
      if (invoiceKind(invoice) === "emitidas") e++;
      else r++;
    }
    return { countEmitidas: e, countRecibidas: r };
  }, [approvedInvoices]);

  const COLUMNS = [
    { key: "fecha_operacion", label: "F. Operación", align: "left" as const },
    { key: "fecha_expedicion", label: "F. Expedición", align: "left" as const },
    { key: "numero_factura", label: "Nº Factura", align: "left" as const },
    { key: "cuenta_contable", label: "Cuenta", align: "left" as const },
    { key: "nombre_contraparte", label: "Contraparte", align: "left" as const },
    { key: "nif_contraparte", label: "NIF Contraparte", align: "left" as const },
    { key: "concepto", label: "Concepto", align: "left" as const },
    { key: "base_euros", label: "Base", align: "right" as const },
    { key: "tipo_porcentaje", label: "IVA %", align: "center" as const },
    { key: "cuota", label: "Cuota", align: "right" as const },
    { key: "total_euros", label: "Total", align: "right" as const },
  ];

  // --- Single-file download ---
  const handleFileDownload = useCallback(
    (file: IntermegaCsvFile) => {
      // Count invoices in this file that lack cuenta_contable, a required
      // Intermega field (FECHA / Nº FACTURA), or a fiscal breakdown.
      let missing = 0;
      let missingReq = 0;
      let missingFiscal = 0;
      for (const [, invoice] of approvedInvoices) {
        const cliente = resolveClienteGestoria(invoice.libro, invoice.formData);
        const nifKey = sanitizeNifKey(cliente.nif);
        const tipo: ExportKind = invoice.libro === "ingresos" ? "emitidas" : "recibidas";
        if (nifKey === file.nifCliente && tipo === file.tipo) {
          if (!invoice.formData.cuenta_contable) missing++;
          if (isMissingRequiredFields(invoice.formData)) missingReq++;
          if (invoice.fiscalLines.length === 0) missingFiscal++;
        }
      }
      if (missing > 0 || missingReq > 0 || missingFiscal > 0) {
        setMissingAccountsCount(missing);
        setMissingFilesCount(missing > 0 ? 1 : 0);
        setMissingRequiredCount(missingReq);
        setMissingRequiredFilesCount(missingReq > 0 ? 1 : 0);
        setMissingFiscalCount(missingFiscal);
        setMissingFiscalFilesCount(missingFiscal > 0 ? 1 : 0);
        setPendingAction({ type: "file", file });
      } else {
        downloadCsvFile(file);
        toast.success(`Descargado: ${file.filename}`);
      }
    },
    [approvedInvoices]
  );

  // --- Bulk ZIP download ---
  const handleZipDownload = useCallback(async () => {
    if (csvFiles.length === 0) return;

    // Count all invoices missing cuenta_contable, a required Intermega field,
    // or a fiscal breakdown.
    let missing = 0;
    let missingReq = 0;
    let missingFiscal = 0;
    const affectedFiles = new Set<string>();
    const affectedReqFiles = new Set<string>();
    const affectedFiscalFiles = new Set<string>();
    for (const [, invoice] of approvedInvoices) {
      const cliente = resolveClienteGestoria(invoice.libro, invoice.formData);
      const nifKey = sanitizeNifKey(cliente.nif);
      const tipo: ExportKind = invoice.libro === "ingresos" ? "emitidas" : "recibidas";
      const key = `${nifKey}__${tipo}`;
      if (!invoice.formData.cuenta_contable) {
        missing++;
        affectedFiles.add(key);
      }
      if (isMissingRequiredFields(invoice.formData)) {
        missingReq++;
        affectedReqFiles.add(key);
      }
      if (invoice.fiscalLines.length === 0) {
        missingFiscal++;
        affectedFiscalFiles.add(key);
      }
    }

    if (missing > 0 || missingReq > 0 || missingFiscal > 0) {
      setMissingAccountsCount(missing);
      setMissingFilesCount(affectedFiles.size);
      setMissingRequiredCount(missingReq);
      setMissingRequiredFilesCount(affectedReqFiles.size);
      setMissingFiscalCount(missingFiscal);
      setMissingFiscalFilesCount(affectedFiscalFiles.size);
      setPendingAction({ type: "zip" });
    } else {
      await doZip();
    }
  }, [approvedInvoices, csvFiles]);

  const doZip = useCallback(async () => {
    // Descarga sin efectos secundarios: la confirmación (persistencia
    // resultado_final.json + maestro + state→done) la dispara el botón
    // explícito "CONFIRMAR Y SEGUIR ESCANEANDO", NO el download.
    try {
      setIsZipping(true);
      await downloadZip(csvFiles);
      toast.success(`ZIP descargado (${csvFiles.length} archivo${csvFiles.length !== 1 ? "s" : ""})`);
    } catch (err) {
      toast.error("Error generando el ZIP");
    } finally {
      setIsZipping(false);
    }
  }, [csvFiles]);

  // --- AlertDialog confirm action ---
  const handleConfirmDownload = useCallback(async () => {
    const action = pendingAction;
    setPendingAction(null);
    if (!action) return;
    if (action.type === "file") {
      downloadCsvFile(action.file);
      toast.success(`Descargado: ${action.file.filename}`);
    } else {
      await doZip();
    }
  }, [pendingAction, doZip]);

  const dialogOpen = pendingAction !== null;
  const isZipAction = pendingAction?.type === "zip";

  const dialogParts: string[] = [];
  if (missingAccountsCount > 0) {
    dialogParts.push(
      isZipAction
        ? `${missingAccountsCount} factura(s) sin cuenta contable asignada en ${missingFilesCount} archivo(s)`
        : `${missingAccountsCount} factura(s) sin cuenta contable asignada en este archivo`
    );
  }
  if (missingFiscalCount > 0) {
    dialogParts.push(
      isZipAction
        ? `${missingFiscalCount} factura(s) sin desglose fiscal (base/IVA) en ${missingFiscalFilesCount} archivo(s) — no generarán filas; Intermega rechazará el fichero`
        : `${missingFiscalCount} factura(s) sin desglose fiscal (base/IVA) — no generarán filas; Intermega rechazará el fichero`
    );
  }
  if (missingRequiredCount > 0) {
    dialogParts.push(
      isZipAction
        ? `${missingRequiredCount} factura(s) sin FECHA o sin Nº FACTURA en ${missingRequiredFilesCount} archivo(s) — Intermega rechazará el fichero`
        : `${missingRequiredCount} factura(s) sin FECHA o sin Nº FACTURA — Intermega rechazará el fichero`
    );
  }
  const dialogDescription = `Hay ${dialogParts.join("; y ")}. Puedes volver a la fase de revisión para corregirlo, o continuar con la descarga.`;

  return (
    <div className="flex flex-col h-full bg-white">
      <AlertDialog open={dialogOpen} onOpenChange={(open) => !open && setPendingAction(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revisar antes de descargar</AlertDialogTitle>
            <AlertDialogDescription>{dialogDescription}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-none text-xs uppercase tracking-[0.15em]">
              Volver a revisión
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDownload}
              className="rounded-none text-xs uppercase tracking-[0.15em]"
            >
              Descargar de todas formas
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Header */}
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            className="text-xs rounded-none uppercase tracking-[0.15em]"
          >
            <ArrowLeft className="h-3 w-3 mr-1" />
            Volver a Revisión
          </Button>
          <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">
            Verificación de Asientos
          </h2>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-bold text-slate-400 font-mono">
            {approvedInvoices.size} factura(s) — {countEmitidas} emitidas · {countRecibidas} recibidas
          </span>
        </div>
      </header>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">

        {/* SIN_CLIENTE warning banner */}
        {hasSinCliente && (
          <div className="flex items-start gap-2 mb-6 px-4 py-3 bg-amber-50 border border-amber-200">
            <AlertTriangle className="h-4 w-4 text-amber-600 mt-0.5 flex-shrink-0" />
            <p className="text-xs text-amber-800">
              <span className="font-black uppercase tracking-[0.1em]">⚠ Hay facturas sin cliente resuelto.</span>{" "}
              Revisa esas facturas antes de importarlas a Intermega — no podrán vincularse a un libro.
            </p>
          </div>
        )}

        {/* Download summary table */}
        {csvFiles.length > 0 && (
          <div className="mb-8">
            <div className="mb-3 px-1">
              <span className="text-[10px] font-black uppercase tracking-[0.15em] text-slate-500">
                Archivos a descargar
              </span>
            </div>
            <div className="border border-slate-200 overflow-hidden">
              <table className="w-full text-xs font-mono border-collapse">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-100 text-[10px] text-slate-400 uppercase font-black">
                    <th className="p-2.5 tracking-widest text-left">NIF Cliente</th>
                    <th className="p-2.5 tracking-widest text-left">Cliente</th>
                    <th className="p-2.5 tracking-widest text-left">Tipo</th>
                    <th className="p-2.5 tracking-widest text-right">Nº Facturas</th>
                    <th className="p-2.5 tracking-widest text-center">Acción</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {csvFiles.map((file) => {
                    const isSinCliente = file.nifCliente === "SIN_CLIENTE";
                    return (
                      <tr
                        key={file.filename}
                        className={cn(
                          "transition-colors",
                          isSinCliente ? "bg-amber-50 hover:bg-amber-100/60" : "hover:bg-slate-50/30"
                        )}
                      >
                        <td className="p-2.5">
                          <span
                            className={cn(
                              "text-[9px] font-black uppercase tracking-[0.1em] px-1.5 py-0.5",
                              isSinCliente
                                ? "bg-amber-100 text-amber-700"
                                : "bg-teal-50 text-teal-700"
                            )}
                          >
                            {isSinCliente ? "SIN CLIENTE" : file.nifCliente}
                          </span>
                        </td>
                        <td className="p-2.5 text-slate-700 max-w-[200px] truncate" title={file.nombreCliente}>
                          {file.nombreCliente || "—"}
                        </td>
                        <td className="p-2.5">
                          <span
                            className={cn(
                              "text-[9px] font-black uppercase tracking-[0.1em] px-1.5 py-0.5",
                              file.tipo === "emitidas"
                                ? "bg-emerald-50 text-emerald-700"
                                : "bg-amber-50 text-amber-700"
                            )}
                          >
                            {file.tipo === "emitidas" ? "Emit." : "Recib."}
                          </span>
                        </td>
                        <td className="p-2.5 text-right text-slate-600">
                          {file.rowCount}
                        </td>
                        <td className="p-2.5 text-center">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleFileDownload(file)}
                            className="h-6 px-2 text-[10px] rounded-none uppercase tracking-[0.1em] text-slate-600 hover:text-slate-900 hover:bg-slate-100"
                          >
                            <Download className="h-3 w-3 mr-1" />
                            Descargar
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Per-client asiento preview */}
        {Array.from(grouped.entries()).map(([nifCliente, group]) => (
          <div key={nifCliente} className="mb-8">
            <div className="flex items-center gap-3 mb-3 px-1">
              <span
                className={cn(
                  "text-[10px] font-black uppercase tracking-[0.15em] px-2 py-1",
                  nifCliente === "SIN_CLIENTE"
                    ? "bg-amber-100 text-amber-700"
                    : "bg-teal-50 text-teal-700"
                )}
              >
                {nifCliente === "SIN_CLIENTE" ? "SIN CLIENTE" : nifCliente}
              </span>
              <span className="text-xs font-bold text-slate-600">{group.nombre}</span>
              <span className="text-[10px] text-slate-400 font-mono">
                ({group.rows.length} línea{group.rows.length !== 1 ? "s" : ""})
              </span>
            </div>

            <div className="border border-slate-200 overflow-hidden overflow-x-auto">
              <table className="w-full text-xs font-mono border-collapse min-w-[900px]">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-100 text-[10px] text-slate-400 uppercase font-black">
                    <th className="p-2.5 tracking-widest whitespace-nowrap text-left">Tipo</th>
                    {COLUMNS.map((col) => (
                      <th
                        key={col.key}
                        className={cn(
                          "p-2.5 tracking-widest whitespace-nowrap",
                          col.align === "right" && "text-right",
                          col.align === "center" && "text-center",
                          col.align === "left" && "text-left"
                        )}
                      >
                        {col.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {group.rows.map((row, idx) => (
                    <tr key={`${row.docId}_${idx}`} className="hover:bg-slate-50/30 transition-colors">
                      <td className="p-2.5">
                        <span
                          className={cn(
                            "text-[9px] font-black uppercase tracking-[0.1em] px-1.5 py-0.5",
                            row.kind === "emitidas"
                              ? "bg-emerald-50 text-emerald-700"
                              : "bg-amber-50 text-amber-700"
                          )}
                        >
                          {row.kind === "emitidas" ? "Emit." : "Recib."}
                        </span>
                      </td>
                      <td className="p-2.5">{row.fecha_operacion || "—"}</td>
                      <td className="p-2.5">{row.fecha_expedicion || "—"}</td>
                      <td className="p-2.5">{row.numero_factura || "—"}</td>
                      <td className={cn("p-2.5", row.cuenta_contable ? "uppercase" : "text-amber-500 italic")}>
                        {row.cuenta_contable || "sin asignar"}
                      </td>
                      <td className="p-2.5 max-w-[150px] truncate" title={row.nombre_contraparte}>
                        {row.nombre_contraparte || "—"}
                      </td>
                      <td className="p-2.5">{row.nif_contraparte || "—"}</td>
                      <td className="p-2.5 max-w-[120px] truncate uppercase" title={row.concepto}>
                        {row.concepto || "—"}
                      </td>
                      <td className="p-2.5 text-right">{row.base_euros}€</td>
                      <td
                        className={cn(
                          "p-2.5 text-center",
                          row.tipo_porcentaje === "EXENTA" && "text-amber-600 text-[10px] font-bold"
                        )}
                      >
                        {row.tipo_porcentaje === "EXENTA"
                          ? "EXENTA"
                          : row.tipo_porcentaje
                          ? `${row.tipo_porcentaje}%`
                          : "—"}
                      </td>
                      <td className="p-2.5 text-right">{row.cuota}€</td>
                      <td className="p-2.5 text-right font-bold">{row.total_euros}€</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}

        {allRows.length === 0 && (
          <div className="flex items-center justify-center h-40 text-slate-400 text-sm">
            No hay facturas aprobadas para verificar.
          </div>
        )}
      </div>

      {/* Footer — dos acciones independientes:
            1) ZIP: descarga local de los CSVs Intermega (sin efectos secundarios).
            2) CONFIRMAR Y SEGUIR ESCANEANDO: persiste resultado_final.json,
               CSV físico y maestro en backend; CTA primario.
        */}
      <footer className="sticky bottom-0 z-20 h-24 flex items-center justify-center gap-3 px-8 bg-white border-t border-slate-200 flex-shrink-0 shadow-[0_-2px_8px_-2px_rgba(0,0,0,0.06)]">
        <Button
          size="lg"
          variant="outline"
          onClick={handleZipDownload}
          disabled={csvFiles.length === 0 || isZipping || isConfirming}
          className={cn(
            "h-11 border-slate-300 text-slate-700 hover:bg-slate-50 font-bold uppercase text-[10px] tracking-[0.2em] rounded-none transition-all px-6",
            (csvFiles.length === 0 || isZipping || isConfirming) && "opacity-50 cursor-not-allowed"
          )}
        >
          {isZipping ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Download className="h-4 w-4 mr-2" />
          )}
          Descargar ZIP ({csvFiles.length})
        </Button>

        <Button
          size="lg"
          onClick={handleConfirmAndContinue}
          disabled={approvedInvoices.size === 0 || isConfirming || isZipping}
          className={cn(
            "h-11 bg-teal-700 border border-teal-700 hover:bg-teal-800 text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all px-8",
            (approvedInvoices.size === 0 || isConfirming || isZipping) && "opacity-50 cursor-not-allowed"
          )}
          title="Persiste el asiento (resultado_final.json + CSV + maestro) y vuelve a Gestión para escanear más facturas"
        >
          {isConfirming ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : null}
          Confirmar y seguir escaneando
        </Button>
      </footer>
    </div>
  );
}
