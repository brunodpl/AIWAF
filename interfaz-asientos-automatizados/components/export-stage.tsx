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
} from "lucide-react";
import { ApprovedInvoiceData } from "@/lib/types";
import {
  generateIntermegaCSV,
  downloadIntermegaEmitidas,
  downloadIntermegaRecibidas,
  normalizeNumber,
} from "@/lib/csv";

type ExportKind = "emitidas" | "recibidas";

interface ExportStageProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  onBack: () => void;
}

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
  kind: ExportKind;
}

function invoiceKind(invoice: ApprovedInvoiceData): ExportKind {
  return invoice.libro === "ingresos" ? "emitidas" : "recibidas";
}

function buildAsientoRows(approvedInvoices: Map<string, ApprovedInvoiceData>): AsientoRow[] {
  const rows: AsientoRow[] = [];

  for (const [docId, invoice] of approvedInvoices) {
    const fd = invoice.formData;
    const nifCliente = fd.nif_cliente || fd.nif_receptor || "";
    const nombreCliente = fd.nombre_cliente || fd.nombre_receptor || "";
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
      nombre_entidad: fd.nombre_entidad || "",
      nif_entidad: fd.nif_entidad || "",
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
    const key = row.nif_cliente || "SIN_CLIENTE";
    if (!groups.has(key)) {
      groups.set(key, { nombre: row.nombre_cliente || "Cliente desconocido", rows: [] });
    }
    groups.get(key)!.rows.push(row);
  }
  return groups;
}

export function ExportStage({ approvedInvoices, onBack }: ExportStageProps) {
  const [downloadingKind, setDownloadingKind] = useState<ExportKind | null>(null);
  const [pendingKind, setPendingKind] = useState<ExportKind | null>(null);
  const [missingAccountsCount, setMissingAccountsCount] = useState(0);

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

  const doDownload = useCallback(
    (kind: ExportKind) => {
      try {
        setDownloadingKind(kind);
        const { emitidas, recibidas } = generateIntermegaCSV(approvedInvoices);
        if (kind === "emitidas") {
          downloadIntermegaEmitidas(emitidas);
          toast.success("Facturas emitidas descargadas");
        } else {
          downloadIntermegaRecibidas(recibidas);
          toast.success("Facturas recibidas descargadas");
        }
      } catch (err) {
        console.error("Error generating Intermega CSV:", err);
        toast.error("Error generando el CSV Intermega");
      } finally {
        setDownloadingKind(null);
      }
    },
    [approvedInvoices]
  );

  const handleDownload = useCallback(
    (kind: ExportKind) => {
      const targetCount = kind === "emitidas" ? countEmitidas : countRecibidas;
      if (targetCount === 0) {
        toast.error(`No hay facturas ${kind} aprobadas para exportar`);
        return;
      }

      let missing = 0;
      for (const [, invoice] of approvedInvoices) {
        if (invoiceKind(invoice) !== kind) continue;
        if (!invoice.formData.cuenta_contable) missing++;
      }

      if (missing > 0) {
        setMissingAccountsCount(missing);
        setPendingKind(kind);
      } else {
        doDownload(kind);
      }
    },
    [approvedInvoices, countEmitidas, countRecibidas, doDownload]
  );

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

  const dialogOpen = pendingKind !== null;

  return (
    <div className="flex flex-col h-full bg-white">
      <AlertDialog open={dialogOpen} onOpenChange={(open) => !open && setPendingKind(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cuentas contables incompletas</AlertDialogTitle>
            <AlertDialogDescription>
              Hay {missingAccountsCount} factura(s) {pendingKind} sin cuenta contable asignada.
              Puedes volver a la fase de revisión para corregirlo, o continuar con la descarga.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-none text-xs uppercase tracking-[0.15em]">
              Volver a revisión
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const k = pendingKind;
                setPendingKind(null);
                if (k) doDownload(k);
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
            {approvedInvoices.size} factura(s) — {countEmitidas} emitidas · {countRecibidas} recibidas
          </span>
        </div>
      </header>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {Array.from(grouped.entries()).map(([nifCliente, group]) => (
          <div key={nifCliente} className="mb-8">
            <div className="flex items-center gap-3 mb-3 px-1">
              <span className="text-[10px] font-black uppercase tracking-[0.15em] text-teal-700 bg-teal-50 px-2 py-1">
                {nifCliente}
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
                      <td
                        className={cn(
                          "p-2.5 text-center",
                          row.tipo_porcentaje === "EXENTA" && "text-amber-600 text-[10px] font-bold"
                        )}
                      >
                        {row.tipo_porcentaje === "EXENTA" ? "EXENTA" : row.tipo_porcentaje ? `${row.tipo_porcentaje}%` : "—"}
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
          onClick={() => handleDownload("emitidas")}
          disabled={countEmitidas === 0 || downloadingKind !== null}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            (countEmitidas === 0 || downloadingKind !== null) && "opacity-50 cursor-not-allowed"
          )}
        >
          {downloadingKind === "emitidas" ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Download className="h-4 w-4 mr-2" />
          )}
          Descargar facturas emitidas ({countEmitidas})
        </Button>
        <Button
          size="lg"
          onClick={() => handleDownload("recibidas")}
          disabled={countRecibidas === 0 || downloadingKind !== null}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            (countRecibidas === 0 || downloadingKind !== null) && "opacity-50 cursor-not-allowed"
          )}
        >
          {downloadingKind === "recibidas" ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Download className="h-4 w-4 mr-2" />
          )}
          Descargar facturas recibidas ({countRecibidas})
        </Button>
      </footer>
    </div>
  );
}
