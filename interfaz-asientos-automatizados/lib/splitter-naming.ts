import type { BatchFileEntry } from "./types";

/**
 * Etiqueta legible de una factura del lote.
 *
 * Si la factura proviene del splitter (`X__NofM.pdf`) y el backend ya nos da
 * `split_origin`/`split_index`/`split_total`, formateamos como
 * `"X.pdf — factura N de M"`. Si no, devolvemos el filename crudo.
 */
export function prettifyBatchFile(entry: BatchFileEntry): string {
  if (entry.split_origin && entry.split_total && entry.split_index) {
    return `${entry.split_origin} — factura ${entry.split_index} de ${entry.split_total}`;
  }
  return entry.filename;
}
