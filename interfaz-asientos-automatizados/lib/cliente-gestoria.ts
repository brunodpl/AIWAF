import type { Libro } from "./types";

/**
 * Maps the libro to which formData fields hold our gestoria's client.
 * Mirrors `LIBRO_A_ROL_CLIENTE` in
 * sistema-de-asientos-automatizado/src/phase4_customer/resolver.py:20-24.
 *
 * Keep these in sync if a new libro is ever added.
 */
const LIBRO_A_ROL: Record<Libro, { nifKey: string; nombreKey: string }> = {
  ingresos: { nifKey: "nif_entidad",  nombreKey: "nombre_entidad" },
  gastos:   { nifKey: "nif_receptor", nombreKey: "nombre_receptor" },
  bienes:   { nifKey: "nif_receptor", nombreKey: "nombre_receptor" },
};

export interface ClienteGestoria {
  nif: string;
  nombre: string;
}

/**
 * Derive the gestoria's client from the current formData + libro.
 *
 * This is reactive: callers should invoke it on every render / before every
 * grouping operation, so that operator edits to `nif_receptor` (gastos/bienes)
 * or `nif_entidad` (ingresos) propagate immediately.
 */
export function resolveClienteGestoria(
  libro: Libro | undefined,
  formData: Record<string, string>
): ClienteGestoria {
  const rol = libro ? LIBRO_A_ROL[libro] : LIBRO_A_ROL.gastos;
  return {
    nif: (formData[rol.nifKey] ?? "").trim(),
    nombre: (formData[rol.nombreKey] ?? "").trim(),
  };
}
