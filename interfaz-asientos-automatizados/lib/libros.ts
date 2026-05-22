import type { Libro, LibroShort } from "./types";

/**
 * Etiquetas canónicas de los libros contables — única fuente de verdad para la
 * UI (F6: antes había 3 redacciones distintas según la pantalla). Forma
 * "emitida/recibida", que explicita la distinción fiscal de Intermega
 * (libros separados de facturas emitidas vs recibidas).
 */
export const LIBRO_LABELS: Record<Libro, string> = {
  gastos: "Recibidas (compras/gastos)",
  ingresos: "Emitidas (ventas/ingresos)",
  bienes: "Bienes de inversión",
};

/** Mapeo forma corta del backend (`compras`/`ventas`/`bienes`) → libro frontend. */
const LIBRO_SHORT_TO_LIBRO: Record<LibroShort, Libro> = {
  compras: "gastos",
  ventas: "ingresos",
  bienes: "bienes",
};

/** Etiqueta canónica para un libro (`Libro` o forma corta `LibroShort`). */
export function libroLabel(libro: Libro | LibroShort | undefined | null): string {
  if (!libro) return "—";
  if (libro in LIBRO_LABELS) return LIBRO_LABELS[libro as Libro];
  const mapped = LIBRO_SHORT_TO_LIBRO[libro as LibroShort];
  return mapped ? LIBRO_LABELS[mapped] : "—";
}
