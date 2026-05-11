"use client";

import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import type { ClientCard, LibroShort } from "@/lib/types";

type Filter = "todos" | "clientes" | "proveedores";

interface ClientsGridProps {
  clients: ClientCard[];
  onSelect: (nif: string) => void;
}

/**
 * Tipo de entidad derivado del NIF (regla simple AEAT):
 *   - Empieza por dígito → PERSONA FÍSICA
 *   - A → SOCIEDAD ANÓNIMA
 *   - B → SOCIEDAD LIMITADA
 *   - resto → entidad
 */
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

function hashColor(nif: string): string {
  // Hash determinístico → paleta clara discreta.
  let h = 0;
  for (let i = 0; i < nif.length; i++) h = (h * 31 + nif.charCodeAt(i)) | 0;
  const palette = [
    "bg-rose-100 text-rose-700",
    "bg-emerald-100 text-emerald-700",
    "bg-sky-100 text-sky-700",
    "bg-amber-100 text-amber-700",
    "bg-violet-100 text-violet-700",
    "bg-teal-100 text-teal-700",
  ];
  return palette[Math.abs(h) % palette.length];
}

function categoryOfClient(c: ClientCard): Filter {
  const hasVentas = c.libros_activos.includes("ventas" as LibroShort);
  const hasCompras = c.libros_activos.includes("compras" as LibroShort);
  if (hasVentas && !hasCompras) return "clientes";
  if (hasCompras && !hasVentas) return "proveedores";
  return "todos";
}

export function ClientsGrid({ clients, onSelect }: ClientsGridProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("todos");

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return clients.filter((c) => {
      const matchesQuery =
        !q ||
        c.nombre.toLowerCase().includes(q) ||
        c.nif.toLowerCase().includes(q);
      if (!matchesQuery) return false;
      if (filter === "todos") return true;
      const cat = categoryOfClient(c);
      // ventas-only → clientes, compras-only → proveedores, mixto → ambos
      const hasVentas = c.libros_activos.includes("ventas" as LibroShort);
      const hasCompras = c.libros_activos.includes("compras" as LibroShort);
      if (filter === "clientes") return hasVentas;
      if (filter === "proveedores") return hasCompras;
      return cat === filter;
    });
  }, [clients, query, filter]);

  if (clients.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-24 text-center text-slate-500">
        <p className="text-sm uppercase tracking-[0.15em]">
          Aún no hay facturas confirmadas
        </p>
        <p className="text-xs mt-2 max-w-md">
          Procesa y confirma facturas en el flujo de escaneo para que aparezcan
          aquí los clientes con su trazabilidad.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row gap-2 sm:items-center sm:justify-between">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar cliente por nombre o NIF..."
            className="pl-8 h-9 text-xs rounded-none"
          />
        </div>
        <div className="flex items-center gap-1">
          {(["todos", "clientes", "proveedores"] as Filter[]).map((f) => (
            <Button
              key={f}
              variant={filter === f ? "default" : "ghost"}
              size="sm"
              onClick={() => setFilter(f)}
              className="text-[10px] uppercase tracking-[0.15em] h-7 px-3 rounded-none"
            >
              {f}
            </Button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {visible.map((c) => {
          const tipo = tipoFromNif(c.nif);
          return (
            <button
              key={c.nif}
              onClick={() => onSelect(c.nif)}
              className="text-left border border-slate-200 hover:border-slate-400 hover:shadow-sm bg-white p-4 transition-colors"
            >
              <div className="flex items-start justify-between gap-2">
                <div
                  className={cn(
                    "flex items-center justify-center w-10 h-10 text-xs font-bold uppercase",
                    hashColor(c.nif),
                  )}
                >
                  {initialsFromName(c.nombre)}
                </div>
                <span className="text-[9px] uppercase tracking-[0.15em] text-slate-400">
                  {tipo}
                </span>
              </div>

              <div className="mt-3">
                <p className="font-medium text-slate-900 leading-tight">
                  {c.nombre}
                </p>
                <p className="text-[10px] uppercase tracking-[0.1em] text-slate-500 mt-1">
                  NIF · {c.nif}
                </p>
              </div>

              <div className="mt-4 flex items-end justify-between">
                <div>
                  <p className="text-[9px] uppercase tracking-[0.15em] text-slate-400">
                    Última factura
                  </p>
                  <p className="text-xs text-slate-700">
                    {c.ultima_factura_fecha ?? "—"}
                  </p>
                </div>
                <div className="text-right">
                  <p className="text-2xl font-bold text-slate-900">
                    {c.documentos_procesados}
                  </p>
                  <p className="text-[9px] uppercase tracking-[0.15em] text-slate-400">
                    Facturas
                  </p>
                </div>
              </div>

              {c.libros_activos.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1">
                  {c.libros_activos.map((lib) => (
                    <span
                      key={lib}
                      className="text-[9px] uppercase tracking-[0.1em] px-1.5 py-0.5 bg-slate-100 text-slate-600"
                    >
                      {lib}
                    </span>
                  ))}
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
