"use client";

import { useState, useCallback } from "react";
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
} from "lucide-react";
import { ApprovedInvoiceData } from "@/lib/types";
import { generateCSV, downloadCSV } from "@/lib/csv";

interface ExportStageProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  onBack: () => void;
}

// Tipo para una fila de la tabla de verificación
interface AsientoRow {
  docId: string;
  nif_cliente: string;
  nombre_cliente: string;
  fecha_operacion: string;
  fecha_expedicion: string;
  numero_factura: string;
  cuenta_contable: string;
  nombre_entidad: string;
  nif_entidad: string;
  concepto: string;
  base_euros: string;
  tipo_porcentaje: string;
  cuota: string;
  total_euros: string;
}

function buildAsientoRows(approvedInvoices: Map<string, ApprovedInvoiceData>): AsientoRow[] {
  const rows: AsientoRow[] = [];

  for (const [docId, invoice] of approvedInvoices) {
    const fd = invoice.formData;
    const nifCliente = fd.nif_cliente || fd.nif_receptor || "";
    const nombreCliente = fd.nombre_cliente || fd.nombre_receptor || "";

    if (invoice.fiscalLines.length > 0) {
      // Una fila por línea fiscal (multi-IVA)
      for (const line of invoice.fiscalLines) {
        rows.push({
          docId,
          nif_cliente: nifCliente,
          nombre_cliente: nombreCliente,
          fecha_operacion: fd.fecha_operacion || "",
          fecha_expedicion: fd.fecha_expedicion || "",
          numero_factura: fd.numero_factura || "",
          cuenta_contable: fd.cuenta_contable || "",
          nombre_entidad: fd.nombre_entidad || "",
          nif_entidad: fd.nif_entidad || "",
          concepto: fd.concepto || "",
          base_euros: Number(line.base).toFixed(2),
          tipo_porcentaje: line.vatRate != null && line.vatRate !== 0 ? String(line.vatRate) : "EXENTA",
          cuota: Number(line.vatAmount).toFixed(2),
          total_euros: Number(line.total).toFixed(2),
        });
      }
    } else {
      // Sin líneas fiscales: una fila con datos de cabecera
      rows.push({
        docId,
        nif_cliente: nifCliente,
        nombre_cliente: nombreCliente,
        fecha_operacion: fd.fecha_operacion || "",
        fecha_expedicion: fd.fecha_expedicion || "",
        numero_factura: fd.numero_factura || "",
        cuenta_contable: fd.cuenta_contable || "",
        nombre_entidad: fd.nombre_entidad || "",
        nif_entidad: fd.nif_entidad || "",
        concepto: fd.concepto || "",
        base_euros: fd.total_euros || "0.00",
        tipo_porcentaje: "",
        cuota: "0.00",
        total_euros: fd.total_euros || "0.00",
      });
    }
  }

  return rows;
}

// Agrupar filas por nif_cliente
function groupByCliente(rows: AsientoRow[]): Map<string, { nombre: string; rows: AsientoRow[] }> {
  const groups = new Map<string, { nombre: string; rows: AsientoRow[] }>();
  for (const row of rows) {
    const key = row.nif_cliente || "SIN_CLIENTE";
    if (!groups.has(key)) {
      groups.set(key, { nombre: row.nombre_cliente || "Cliente desconocido", rows: [] });
    }
    groups.get(key)!.rows.push(row);
  }
  return groups;
}

export function ExportStage({ approvedInvoices, onBack }: ExportStageProps) {
  const [downloading, setDownloading] = useState(false);
  const [showMissingAccountsDialog, setShowMissingAccountsDialog] = useState(false);
  const [missingAccountsCount, setMissingAccountsCount] = useState(0);

  const allRows = buildAsientoRows(approvedInvoices);
  const grouped = groupByCliente(allRows);

  const doDownload = useCallback(() => {
    try {
      setDownloading(true);
      // generateCSV espera clients Map — pasamos vacío (las cuentas ya están en formData)
      const csvContent = generateCSV(approvedInvoices, new Map());
      downloadCSV(csvContent);
      toast.success("CSV descargado correctamente");
    } catch (err) {
      console.error("Error generating CSV:", err);
      toast.error("Error generando el CSV");
    } finally {
      setDownloading(false);
    }
  }, [approvedInvoices]);

  const handleDownloadCSV = useCallback(() => {
    if (approvedInvoices.size === 0) {
      toast.error("No hay facturas aprobadas para exportar");
      return;
    }

    // Comprobar cuentas contables vacías
    let missing = 0;
    for (const [, invoice] of approvedInvoices) {
      if (!invoice.formData.cuenta_contable) {
        missing++;
      }
    }

    if (missing > 0) {
      setMissingAccountsCount(missing);
      setShowMissingAccountsDialog(true);
    } else {
      doDownload();
    }
  }, [approvedInvoices, doDownload]);

  const COLUMNS = [
    { key: "fecha_operacion", label: "F. Operación", align: "left" as const },
    { key: "fecha_expedicion", label: "F. Expedición", align: "left" as const },
    { key: "numero_factura", label: "Nº Factura", align: "left" as const },
    { key: "cuenta_contable", label: "Cuenta", align: "left" as const },
    { key: "nombre_entidad", label: "Entidad", align: "left" as const },
    { key: "nif_entidad", label: "NIF Entidad", align: "left" as const },
    { key: "concepto", label: "Concepto", align: "left" as const },
    { key: "base_euros", label: "Base", align: "right" as const },
    { key: "tipo_porcentaje", label: "IVA %", align: "center" as const },
    { key: "cuota", label: "Cuota", align: "right" as const },
    { key: "total_euros", label: "Total", align: "right" as const },
  ];

  return (
    <div className="flex flex-col h-full bg-white">
      {/* AlertDialog para cuentas faltantes */}
      <AlertDialog open={showMissingAccountsDialog} onOpenChange={setShowMissingAccountsDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cuentas contables incompletas</AlertDialogTitle>
            <AlertDialogDescription>
              Hay {missingAccountsCount} factura(s) sin cuenta contable asignada.
              Puedes volver a la fase de revisión para corregirlo, o continuar con la descarga.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-none text-xs uppercase tracking-[0.15em]">
              Volver a revisión
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setShowMissingAccountsDialog(false);
                doDownload();
              }}
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
            {approvedInvoices.size} factura(s) — {allRows.length} línea(s) de asiento
          </span>
        </div>
      </header>

      {/* Content — tabla de verificación agrupada por cliente */}
      <div className="flex-1 overflow-auto p-6">
        {Array.from(grouped.entries()).map(([nifCliente, group]) => (
          <div key={nifCliente} className="mb-8">
            {/* Header de grupo: cliente */}
            <div className="flex items-center gap-3 mb-3 px-1">
              <span className="text-[10px] font-black uppercase tracking-[0.15em] text-teal-700 bg-teal-50 px-2 py-1">
                {nifCliente}
              </span>
              <span className="text-xs font-bold text-slate-600">
                {group.nombre}
              </span>
              <span className="text-[10px] text-slate-400 font-mono">
                ({group.rows.length} línea{group.rows.length !== 1 ? "s" : ""})
              </span>
            </div>

            {/* Tabla del grupo */}
            <div className="border border-slate-200 overflow-hidden overflow-x-auto">
              <table className="w-full text-xs font-mono border-collapse min-w-[900px]">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-100 text-[10px] text-slate-400 uppercase font-black">
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
                      <td className="p-2.5">{row.fecha_operacion || "—"}</td>
                      <td className="p-2.5">{row.fecha_expedicion || "—"}</td>
                      <td className="p-2.5">{row.numero_factura || "—"}</td>
                      <td className={cn("p-2.5", !row.cuenta_contable && "text-amber-500 italic")}>
                        {row.cuenta_contable || "sin asignar"}
                      </td>
                      <td className="p-2.5 max-w-[150px] truncate" title={row.nombre_entidad}>
                        {row.nombre_entidad || "—"}
                      </td>
                      <td className="p-2.5">{row.nif_entidad || "—"}</td>
                      <td className="p-2.5 max-w-[120px] truncate" title={row.concepto}>
                        {row.concepto || "—"}
                      </td>
                      <td className="p-2.5 text-right">{row.base_euros}€</td>
                      <td className={cn(
                        "p-2.5 text-center",
                        row.tipo_porcentaje === "EXENTA" && "text-amber-600 text-[10px] font-bold"
                      )}>
                        {row.tipo_porcentaje === "EXENTA" ? "EXENTA" : `${row.tipo_porcentaje}%`}
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

      {/* Footer */}
      <footer className="h-24 flex items-center justify-center gap-6 px-8 bg-white border-t flex-shrink-0">
        <Button
          size="lg"
          onClick={handleDownloadCSV}
          disabled={approvedInvoices.size === 0 || downloading}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            (approvedInvoices.size === 0 || downloading) && "opacity-50 cursor-not-allowed"
          )}
        >
          {downloading ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Download className="h-4 w-4 mr-2" />
          )}
          Descargar CSV
        </Button>
      </footer>
    </div>
  );
}
