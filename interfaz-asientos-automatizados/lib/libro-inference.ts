import type { Libro } from "./types";
import { CUENTA_BY_CODE } from "./cuentas-maestro";

const GROUP_TO_LIBRO: Record<"2" | "6" | "7", Libro> = {
  "2": "bienes",
  "6": "gastos",
  "7": "ingresos",
};

const PREFIX_TO_LIBRO: Record<string, Libro> = {
  "2": "bienes",
  "6": "gastos",
  "7": "ingresos",
};

/**
 * Infiere el libro a partir de una cuenta contable PGC.
 *
 * Reglas (deterministas):
 *   1. Si la cuenta está en el maestro (`CUENTA_BY_CODE`), usa su `group`.
 *   2. Si es custom, usa el primer carácter del código:
 *        "7" → ingresos · "6" → gastos · "2" → bienes
 *      Cualquier otro prefijo (4, 5, letra, símbolo) devuelve null para
 *      evitar cambiar el libro ante cuentas ambiguas.
 *   3. Vacío / whitespace → null (no tocar libro durante edición intermedia).
 */
export function inferLibroFromCuenta(code: string | undefined | null): Libro | null {
  if (!code) return null;
  const trimmed = code.trim();
  if (!trimmed) return null;

  const known = CUENTA_BY_CODE[trimmed];
  if (known) return GROUP_TO_LIBRO[known.group];

  const first = trimmed[0];
  return PREFIX_TO_LIBRO[first] ?? null;
}

/**
 * Etiqueta humana para un libro — usada en banner sticky y toasts del reviewer.
 */
export function libroLabel(libro: Libro | undefined): string {
  if (libro === "ingresos") return "Emitida (ventas/ingresos)";
  if (libro === "gastos") return "Recibida (compras/gastos)";
  if (libro === "bienes") return "Bienes de inversión";
  return "—";
}
