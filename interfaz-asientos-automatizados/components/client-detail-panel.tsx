"use client";

import { useEffect, useMemo, useState, useCallback } from "react";
import { X, ChevronRight, ChevronDown, ArrowLeft, Download } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { ClientCard, ClientInvoice, LibroShort } from "@/lib/types";
import { fetchClientInvoices } from "@/lib/api-clients";

interface ClientDetailPanelProps {
  nif: string;
  clientCard: ClientCard | null;
  onClose: () => void;
}

function badgeForInvoice(inv: ClientInvoice): {
  label: string;
  className: string;
} {
  if (inv.decision_global === "auto" && !inv.tiene_ediciones) {
    return { label: "Auto", className: "bg-emerald-100 text-emerald-700" };
  }
  if (inv.decision_global === "warn" || inv.tiene_ediciones) {
    return { label: "Revisado", className: "bg-amber-100 text-amber-700" };
  }
  return { label: "Pendiente", className: "bg-slate-100 text-slate-500" };
}

function libroLabel(libro: LibroShort | null | string | null): string {
  if (libro === "ventas") return "Emit.";
  if (libro === "bienes") return "Bien.";
  return "Recib.";
}

function tipoFromNif(nif: string): string {
  const first = (nif?.[0] ?? "").toUpperCase();
  if (/[0-9]/.test(first)) return "Persona física";
  if (first === "A") return "Sociedad anónima";
  if (first === "B") return "Sociedad limitada";
  if (first === "X" || first === "Y" || first === "Z") return "Persona física";
  return "Entidad";
}

function initialsFromName(nombre: string): string {
  const parts = (nombre || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "??";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function formatEuros(v: number): string {
  return `${v.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} €`;
}

function escapeCsv(v: string | number | null | undefined): string {
  if (v == null) return "";
  const s = String(v);
  if (s.includes(";") || s.includes('"') || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function MetadataField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | number | null | undefined;
  edited?: boolean;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-[0.12em] text-slate-400">
        {label}
      </span>
      <span
        className={cn(
          "text-sm text-slate-700",
          mono && "font-mono",
          !value && "text-slate-300 italic",
        )}
      >
        {value ?? "—"}
      </span>
    </div>
  );
}

export function ClientDetailPanel({ nif, clientCard, onClose }: ClientDetailPanelProps) {
  const [invoices, setInvoices] = useState<ClientInvoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchClientInvoices(nif)
      .then((d) => {
        if (!cancelled) setInvoices(d.invoices);
      })
      .catch((e) => {
        console.error("[historial] error cargando facturas:", e);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nif]);

  const toggleExpand = useCallback((docId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  }, []);

  const grouped = useMemo(() => {
    const map = new Map<string, ClientInvoice[]>();
    for (const inv of invoices) {
      const k = inv.fecha_operacion ?? inv.fecha_expedicion ?? "sin-fecha";
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(inv);
    }
    return Array.from(map.entries()).sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0));
  }, [invoices]);

  const downloadCsv = useCallback(() => {
    const headers = [
      "doc_id", "numero_factura", "libro", "fecha_operacion", "fecha_expedicion",
      "total_euros", "concepto", "cuenta_contable", "decision_global",
      "contraparte_nif", "contraparte_nombre",
    ];
    const rows = invoices.map((inv) =>
      [
        inv.doc_id, inv.numero_factura, inv.libro, inv.fecha_operacion,
        inv.fecha_expedicion, inv.total_euros, inv.concepto, inv.cuenta_contable,
        inv.decision_global, inv.contraparte_nif, inv.contraparte_nombre,
      ].map(escapeCsv).join(";"),
    );
    const csv = "﻿" + headers.join(";") + "\n" + rows.join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `historial_${nif}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [invoices, nif]);

  const isEdited = (inv: ClientInvoice, field: string) =>
    inv.campos_editados?.includes(field) ?? false;

  return (
    <div className="fixed inset-0 z-50 flex">
      <div
        className="flex-1 bg-black/30"
        onClick={onClose}
        aria-label="Cerrar panel"
      />
      <aside className="w-full sm:w-[780px] bg-white shadow-xl overflow-y-auto flex flex-col">
        <header className="flex-shrink-0 sticky top-0 bg-white border-b border-slate-100 px-6 py-4 z-10">
          <div className="flex items-center justify-between mb-3">
            <button
              onClick={onClose}
              className="inline-flex items-center gap-1 text-[11px] uppercase tracking-[0.15em] text-slate-500 hover:text-slate-900"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> Volver a clientes
            </button>
            <button
              onClick={onClose}
              className="text-slate-400 hover:text-slate-700"
              aria-label="Cerrar"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="flex items-start gap-3">
            <div className="flex items-center justify-center w-10 h-10 bg-rose-100 text-rose-700 text-xs font-bold uppercase">
              {initialsFromName(clientCard?.nombre ?? nif)}
            </div>
            <div className="flex-1">
              <p className="text-[10px] uppercase tracking-[0.15em] text-slate-400">
                {tipoFromNif(nif)}
              </p>
              <h2 className="font-semibold text-lg text-slate-900 leading-tight">
                {clientCard?.nombre ?? nif}
              </h2>
              <p className="text-[11px] uppercase tracking-[0.1em] text-slate-500 mt-1">
                NIF · {nif} · Facturas con asiento {invoices.length} ·{" "}
                Última {clientCard?.ultima_factura_fecha ?? "—"}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="text-[11px] uppercase tracking-[0.15em] h-8 px-3 rounded-none gap-1.5"
              onClick={downloadCsv}
              disabled={invoices.length === 0}
            >
              <Download className="h-3.5 w-3.5" />
              Exportar listado
            </Button>
          </div>
        </header>

        <div className="flex-1 px-6 py-4 space-y-6">
          {loading && (
            <p className="text-sm text-slate-400 text-center mt-12">Cargando...</p>
          )}

          {!loading && invoices.length === 0 && (
            <p className="text-sm text-slate-400 text-center mt-12">
              Sin facturas confirmadas para este cliente.
            </p>
          )}

          {grouped.map(([fecha, items]) => (
            <section key={fecha}>
              <div className="flex items-center justify-between border-b border-slate-200 pb-1 mb-2">
                <p className="text-xs font-medium text-slate-700">{fecha}</p>
                <p className="text-[10px] uppercase tracking-[0.15em] text-slate-400">
                  {items.length} {items.length === 1 ? "factura" : "facturas"}
                </p>
              </div>

              <ul className="space-y-1">
                {items.map((inv) => {
                  const badge = badgeForInvoice(inv);
                  const importeNeg = inv.total_euros < 0;
                  const isExpanded = expandedIds.has(inv.doc_id);
                  const lineas = inv.lineas_asiento ?? [];
                  return (
                    <li key={inv.doc_id} className="border border-slate-100 hover:border-slate-300">
                      <button
                        type="button"
                        onClick={() => toggleExpand(inv.doc_id)}
                        className="w-full grid grid-cols-[auto_auto_1fr_auto_auto] items-center gap-3 px-3 py-2.5 text-left"
                      >
                        <span className="text-[10px] uppercase tracking-[0.1em] px-1.5 py-0.5 bg-slate-100 text-slate-600">
                          {libroLabel(inv.libro)}
                        </span>
                        <div className="flex flex-col min-w-[80px]">
                          <span className="text-sm font-mono text-slate-700 truncate">
                            {inv.numero_factura ?? inv.doc_id}
                          </span>
                          {inv.contraparte_nombre && (
                            <span className="text-[10px] uppercase tracking-[0.1em] text-slate-400 truncate">
                              {inv.libro === "ventas" ? "Cliente" : "Proveedor"} · {inv.contraparte_nombre}
                            </span>
                          )}
                        </div>
                        <span
                          className={cn(
                            "text-[10px] uppercase tracking-[0.15em] px-1.5 py-0.5",
                            badge.className,
                          )}
                        >
                          {badge.label}
                        </span>
                        <span
                          className={cn(
                            "text-sm text-right",
                            importeNeg ? "text-rose-600" : "text-slate-700",
                          )}
                        >
                          {formatEuros(inv.total_euros)}
                        </span>
                        {isExpanded ? (
                          <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
                        )}
                      </button>

                      {isExpanded && (
                        <div className="px-4 pb-4 border-t border-slate-100">
                          {/* Sección A — Metadatos */}
                          <div className="grid grid-cols-3 gap-x-6 gap-y-3 mt-3 mb-4 p-3 bg-slate-50 rounded">
                            <MetadataField
                              label="Factura"
                              value={inv.numero_factura}
                              edited={isEdited(inv, "numero_factura")}
                              mono
                            />
                            <MetadataField
                              label="Fecha operación"
                              value={inv.fecha_operacion}
                              edited={isEdited(inv, "fecha_operacion")}
                            />
                            <MetadataField
                              label="Fecha expedición"
                              value={inv.fecha_expedicion}
                              edited={isEdited(inv, "fecha_expedicion")}
                            />
                            <MetadataField
                              label="Concepto"
                              value={inv.concepto}
                              edited={isEdited(inv, "concepto")}
                            />
                            <MetadataField
                              label="Cuenta contable"
                              value={inv.cuenta_contable}
                              edited={isEdited(inv, "cuenta_contable")}
                              mono
                            />
                            <MetadataField
                              label="Total"
                              value={formatEuros(inv.total_euros)}
                              edited={isEdited(inv, "total_euros")}
                            />
                            <MetadataField
                              label="Emisor (NIF)"
                              value={inv.nif_entidad}
                              edited={isEdited(inv, "nif_entidad")}
                              mono
                            />
                            <MetadataField
                              label="Emisor (nombre)"
                              value={inv.nombre_entidad}
                              edited={isEdited(inv, "nombre_entidad")}
                            />
                            <MetadataField
                              label="Libro"
                              value={inv.libro}
                            />
                            <MetadataField
                              label="Receptor (NIF)"
                              value={inv.nif_receptor}
                              edited={isEdited(inv, "nif_receptor")}
                              mono
                            />
                            <MetadataField
                              label="Receptor (nombre)"
                              value={inv.nombre_receptor}
                              edited={isEdited(inv, "nombre_receptor")}
                            />
                            <MetadataField
                              label="Decisión"
                              value={inv.decision_global}
                            />
                          </div>

                          {/* Sección B — Líneas de asiento */}
                          {lineas.length === 0 ? (
                            <p className="text-[11px] text-slate-400 mt-2">
                              Sin líneas de asiento registradas.
                            </p>
                          ) : (
                            <table className="w-full text-[11px] mt-1">
                              <thead>
                                <tr className="text-slate-400 uppercase tracking-[0.1em] border-b border-slate-200">
                                  <th className="text-left py-1.5 pr-2 font-medium">Cuenta</th>
                                  <th className="text-left py-1.5 pr-2 font-medium">Concepto</th>
                                  <th className="text-right py-1.5 pr-2 font-medium">Debe</th>
                                  <th className="text-right py-1.5 pr-2 font-medium">Haber</th>
                                  <th className="text-right py-1.5 pr-2 font-medium">Tipo IVA</th>
                                  <th className="text-right py-1.5 font-medium">Base Imp.</th>
                                </tr>
                              </thead>
                              <tbody>
                                {lineas.map((l, idx) => (
                                  <tr
                                    key={idx}
                                    className="border-b border-slate-50 text-slate-700"
                                  >
                                    <td className="py-1.5 pr-2 font-mono">{l.cuenta}</td>
                                    <td className="py-1.5 pr-2">{l.concepto}</td>
                                    <td className="py-1.5 pr-2 text-right">
                                      {l.debe > 0 ? formatEuros(l.debe) : "—"}
                                    </td>
                                    <td className="py-1.5 pr-2 text-right">
                                      {l.haber > 0 ? formatEuros(l.haber) : "—"}
                                    </td>
                                    <td className="py-1.5 pr-2 text-right">
                                      {l.tipo_iva != null ? `${l.tipo_iva}%` : "—"}
                                    </td>
                                    <td className="py-1.5 text-right">
                                      {l.base_imponible != null ? formatEuros(l.base_imponible) : "—"}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      </aside>
    </div>
  );
}
