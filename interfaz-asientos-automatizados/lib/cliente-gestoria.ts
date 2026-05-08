import type { Libro } from "./types";

/**
 * Maps the libro to which formData fields hold our gestoria's client.
 *
 * Mirrors the SEMANTICS of `LIBRO_A_ROL_CLIENTE` in
 * sistema-de-asientos-automatizado/src/phase4_customer/resolver.py:20-24.
 *
 * Note the key-naming difference:
 *   backend keys → "20_COMPRAS_GASTOS" / "21_VENTAS_INGRESOS" / "22_BIENES_INVERSION"
 *   frontend keys → "gastos"           / "ingresos"           / "bienes"
 *
 * If a new libro is added to the backend (e.g. "23_INTRACOMUNITARIAS"):
 *   1. Add the corresponding member to the `Libro` union in `lib/types.ts`.
 *   2. Add an entry here mapping it to the right (nifKey, nombreKey).
 *   3. Confirm the export pipeline (csv.ts → tipo, export-stage banner) handles it.
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
