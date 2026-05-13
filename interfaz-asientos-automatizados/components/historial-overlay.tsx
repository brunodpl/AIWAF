"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { toast } from "sonner";
import { ClientsGrid } from "./clients-grid";
import { ClientDetailPanel } from "./client-detail-panel";
import type { ClientCard } from "@/lib/types";
import { fetchClients } from "@/lib/api-clients";

interface HistorialOverlayProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Vista de trazabilidad por cliente como **overlay** sobre la app principal.
 *
 * Diseño: es independiente del flujo Gestión→Escaneando→Revisión→Exportar. Al
 * cerrar se vuelve exactamente al stage donde estaba el operario, sin pasar
 * por localStorage ni perder estado en memoria. Por eso vive como modal a
 * pantalla casi completa y NO como `/historial` route.
 */
export function HistorialOverlay({ open, onClose }: HistorialOverlayProps) {
  const [clients, setClients] = useState<ClientCard[]>([]);
  const [selectedNif, setSelectedNif] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Carga al abrir, recarga cada vez para reflejar confirmaciones recientes.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setSelectedNif(null);
    fetchClients()
      .then((d) => {
        if (!cancelled) setClients(d.clients);
      })
      .catch((e) => {
        console.error("[historial] error cargando clientes:", e);
        toast.error("No se pudo cargar la trazabilidad");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // ESC cierra el overlay.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-40 bg-slate-50 flex flex-col"
      role="dialog"
      aria-modal="true"
      aria-label="Trazabilidad por cliente"
    >
      <header className="flex-shrink-0 flex items-center justify-between px-6 py-3 bg-white border-b border-slate-100">
        <h1 className="text-[10px] uppercase tracking-[0.2em] text-slate-500">
          Trazabilidad por cliente
        </h1>
        <div className="flex items-center gap-4">
          <span className="text-[10px] text-slate-400">
            {clients.length} {clients.length === 1 ? "cliente" : "clientes"}
          </span>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-700 inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.15em]"
            aria-label="Cerrar trazabilidad"
            title="Volver al flujo de escaneo (ESC)"
          >
            Cerrar <X className="h-3 w-3" />
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        {loading ? (
          <p className="text-xs text-slate-400 text-center mt-12">Cargando...</p>
        ) : (
          <ClientsGrid clients={clients} onSelect={setSelectedNif} />
        )}
      </div>

      {selectedNif && (
        <ClientDetailPanel
          nif={selectedNif}
          clientCard={clients.find((c) => c.nif === selectedNif) ?? null}
          onClose={() => setSelectedNif(null)}
        />
      )}
    </div>
  );
}
