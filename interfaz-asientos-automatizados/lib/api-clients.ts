/**
 * API client de trazabilidad por cliente (Bloque /historial).
 *
 * Endpoints backend:
 *   GET  /api/clients                        → grid de tarjetas
 *   GET  /api/clients/{nif}/invoices         → panel lateral
 *   POST /api/pipeline/confirm               → confirmar lote
 */

import type {
  ClientCard,
  ClientInvoice,
  ConfirmBatchPayload,
  ConfirmBatchResponse,
} from "./types";

const API_TIMEOUT_MS = 30000;

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  try {
    const r = await fetch(url, { ...init, signal: controller.signal });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      throw new Error(`HTTP ${r.status} ${url} — ${text}`);
    }
    return (await r.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function fetchClients(): Promise<{ clients: ClientCard[]; total: number }> {
  return fetchJSON("/api/clients");
}

export async function fetchClientInvoices(
  nif: string,
): Promise<{ nif: string; invoices: ClientInvoice[] }> {
  const safe = encodeURIComponent(nif);
  return fetchJSON(`/api/clients/${safe}/invoices`);
}

export async function confirmBatch(
  payload: ConfirmBatchPayload,
): Promise<ConfirmBatchResponse> {
  return fetchJSON("/api/pipeline/confirm", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * Construye un CSV mínimo Intermega-compat desde las líneas de asiento y lo
 * devuelve como base64. El backend recibirá el blob ya codificado vía
 * ConfirmBatchPayload.asientos[doc_id].csv_b64.
 *
 * Formato: separador ";", cabecera, una línea por movimiento.
 */
export function buildCsvBase64(
  lineas: Array<{
    cuenta: string;
    concepto: string;
    debe: number;
    haber: number;
    tipo_iva?: number;
    base_imponible?: number;
  }>,
): string {
  const header = "cuenta;concepto;debe;haber;tipo_iva;base_imponible";
  const rows = lineas.map(
    (l) =>
      `${l.cuenta};${l.concepto};${(l.debe ?? 0).toFixed(2)};${(l.haber ?? 0).toFixed(2)};${l.tipo_iva ?? ""};${(l.base_imponible ?? 0).toFixed(2)}`,
  );
  const csv = [header, ...rows].join("\n") + "\n";
  // btoa no maneja caracteres no-ASCII; encodeURIComponent → escape → bytes.
  return typeof window !== "undefined"
    ? window.btoa(unescape(encodeURIComponent(csv)))
    : Buffer.from(csv, "utf-8").toString("base64");
}
