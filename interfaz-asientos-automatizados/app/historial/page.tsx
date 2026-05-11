"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { StageIndicator } from "@/components/stage-indicator";
import { FeedbackButton } from "@/components/feedback-button";
import { ClientsGrid } from "@/components/clients-grid";
import { ClientDetailPanel } from "@/components/client-detail-panel";
import type { ClientCard } from "@/lib/types";
import { fetchClients } from "@/lib/api-clients";

/**
 * /historial — trazabilidad por cliente.
 *
 * Grid de tarjetas (lectura de maestro_clientes.yaml vía /api/clients) +
 * panel lateral con facturas confirmadas del NIF seleccionado. No participa
 * en el flujo de 4 etapas (Gestión/Escaneando/Revisión/Exportar): es una
 * vista de consulta accesible desde el botón HISTORIAL del header.
 */
export default function HistorialPage() {
  const [clients, setClients] = useState<ClientCard[]>([]);
  const [selectedNif, setSelectedNif] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchClients()
      .then((data) => {
        if (!cancelled) setClients(data.clients);
      })
      .catch((err) => {
        console.error("[historial] error cargando clientes:", err);
        toast.error("No se pudo cargar la lista de clientes");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="w-full h-screen bg-slate-50 overflow-hidden flex flex-col">
      <div className="flex-shrink-0 px-6 py-2 bg-white border-b border-slate-100">
        <StageIndicator stage="books" />
      </div>

      <div className="flex-shrink-0 px-6 py-3 bg-white border-b border-slate-100 flex items-center justify-between">
        <Link
          href="/"
          className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.15em] text-slate-500 hover:text-slate-900"
        >
          <ArrowLeft className="h-3 w-3" /> Volver a gestión
        </Link>
        <h1 className="text-[10px] uppercase tracking-[0.2em] text-slate-500">
          Trazabilidad por cliente
        </h1>
        <span className="text-[10px] text-slate-400">
          {clients.length} {clients.length === 1 ? "cliente" : "clientes"}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        {loading ? (
          <p className="text-xs text-slate-400 text-center mt-12">Cargando...</p>
        ) : (
          <ClientsGrid clients={clients} onSelect={setSelectedNif} />
        )}
      </div>

      <FeedbackButton />
      <Toaster />

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
