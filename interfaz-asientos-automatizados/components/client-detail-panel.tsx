"use client";

import { useEffect, useMemo, useState } from "react";
import { X, ChevronRight, ArrowLeft } from "lucide-react";
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
  // AUTO verde, REVISADO amarillo, PENDIENTE gris.
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

export function ClientDetailPanel({ nif, clientCard, onClose }: ClientDetailPanelProps) {
  const [invoices, setInvoices] = useState<ClientInvoice[]>([]);
  const [loading, setLoading] = useState(true);

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

  // Agrupar por fecha_expedicion (YYYY-MM-DD).
  const grouped = useMemo(() => {
    const map = new Map<string, ClientInvoice[]>();
    for (const inv of invoices) {
      const k = inv.fecha_expedicion || "sin-fecha";
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(inv);
    }
    return Array.from(map.entries()).sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0));
  }, [invoices]);

  return (
    <div className="fixed inset-0 z-50 flex">
      <div
        className="flex-1 bg-black/30"
        onClick={onClose}
        aria-label="Cerrar panel"
      />
      <aside className="w-full sm:w-[640px] bg-white shadow-xl overflow-y-auto flex flex-col">
        <header className="flex-shrink-0 sticky top-0 bg-white border-b border-slate-100 px-6 py-4 z-10">
          <div className="flex items-center justify-between mb-3">
            <button
              onClick={onClose}
              className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-slate-500 hover:text-slate-900"
            >
              <ArrowLeft className="h-3 w-3" /> Volver a clientes
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
              <p className="text-[9px] uppercase tracking-[0.15em] text-slate-400">
                {tipoFromNif(nif)}
              </p>
              <h2 className="font-semibold text-slate-900 leading-tight">
                {clientCard?.nombre ?? nif}
              </h2>
              <p className="text-[10px] uppercase tracking-[0.1em] text-slate-500 mt-1">
                NIF · {nif} · Facturas con asiento {invoices.length} ·{" "}
                Última {clientCard?.ultima_factura_fecha ?? "—"}
              </p>
            </div>
            <div className="flex flex-col gap-1">
              <Button
                size="sm"
                variant="outline"
                className="text-[10px] uppercase tracking-[0.15em] h-7 px-2 rounded-none"
                disabled
                title="Disponible próximamente"
              >
                Exportar listado
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="text-[10px] uppercase tracking-[0.15em] h-7 px-2 rounded-none"
                disabled
                title="Disponible próximamente"
              >
                Ver libro
              </Button>
            </div>
          </div>
        </header>

        <div className="flex-1 px-6 py-4 space-y-6">
          {loading && (
            <p className="text-xs text-slate-400 text-center mt-12">Cargando...</p>
          )}

          {!loading && invoices.length === 0 && (
            <p className="text-xs text-slate-400 text-center mt-12">
              Sin facturas confirmadas para este cliente.
            </p>
          )}

          {grouped.map(([fecha, items]) => (
            <section key={fecha}>
              <div className="flex items-center justify-between border-b border-slate-200 pb-1 mb-2">
                <p className="text-[11px] font-medium text-slate-700">{fecha}</p>
                <p className="text-[9px] uppercase tracking-[0.15em] text-slate-400">
                  {items.length} {items.length === 1 ? "factura" : "facturas"}
                </p>
              </div>

              <ul className="space-y-1">
                {items.map((inv) => {
                  const badge = badgeForInvoice(inv);
                  const importeNeg = inv.total_euros < 0;
                  return (
                    <li
                      key={inv.doc_id}
                      className="grid grid-cols-[auto_auto_1fr_auto_auto] items-center gap-3 px-3 py-2 border border-slate-100 hover:border-slate-300"
                    >
                      <span className="text-[9px] uppercase tracking-[0.1em] px-1.5 py-0.5 bg-slate-100 text-slate-600">
                        {libroLabel(inv.libro)}
                      </span>
                      <span className="text-xs font-mono text-slate-700 min-w-[80px]">
                        {inv.numero_factura ?? inv.doc_id}
                      </span>
                      <span
                        className={cn(
                          "text-[9px] uppercase tracking-[0.15em] px-1.5 py-0.5",
                          badge.className,
                        )}
                      >
                        {badge.label}
                      </span>
                      <span
                        className={cn(
                          "text-xs text-right",
                          importeNeg ? "text-rose-600" : "text-slate-700",
                        )}
                      >
                        {formatEuros(inv.total_euros)}
                      </span>
                      <ChevronRight className="h-3 w-3 text-slate-400" />
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
