/**
 * Fixtures y helpers compartidos para la suite multi-invoice.
 *
 * Convenciones:
 * - Una factura del PDF de prueba aparece como un `InvoiceListItem` en
 *   `/api/invoices`. Si el splitter dividió bien, hay 3 items con doc_id
 *   `3_facturas__1of3`, `__2of3`, `__3of3`. Si no dividió, hay 1 con
 *   doc_id `3_facturas`.
 * - El backend acepta uploads vía POST /api/books/{bookId}/upload donde
 *   bookId ∈ {gastos, ingresos, bienes} (forma frontend).
 * - `resetPipeline` borra libros/asientos/ pero NO los PDFs en libros/facturas/.
 *   Para limpiar PDFs (idempotencia entre tests) hay que borrarlos via
 *   DELETE /api/books/{bookId}/files/{filename} ANTES del reset.
 */

import path from "node:path";
import { promises as fs } from "node:fs";
import {
  test as base,
  APIRequestContext,
  Page,
  TestInfo,
  expect,
} from "@playwright/test";

export const ASSET_DIR = path.join(__dirname, "__assets__");
export const MULTI_INVOICE_PDF = path.join(ASSET_DIR, "3_facturas.pdf");
export const MULTI_INVOICE_FILENAME = "3_facturas.pdf";

export type BookId = "gastos" | "ingresos" | "bienes";

export const BOOK_LABELS: Record<BookId, RegExp> = {
  gastos: /compras|gastos/i,
  ingresos: /ventas|ingresos/i,
  bienes: /bienes|inversi/i,
};

/**
 * Borra todos los archivos pendientes del libro indicado (best-effort).
 * Útil para garantizar que un test arranca con el inbox limpio sin
 * depender de que reset toque libros/facturas/.
 */
export async function clearBookInbox(api: APIRequestContext, bookId: BookId): Promise<void> {
  const res = await api.get(`/api/books`);
  if (!res.ok()) return;
  const body = await res.json();
  const book = (body.books ?? []).find((b: { id: string }) => b.id === bookId);
  if (!book) return;
  for (const f of book.files ?? []) {
    // Saltamos los que estén en estado terminal (split/confirmed/done) — el
    // backend los rechazaría con 409 y no queremos esconder esos casos.
    if (["confirmed", "split"].includes(f.status)) continue;
    const encoded = encodeURIComponent(f.name);
    await api.delete(`/api/books/${bookId}/files/${encoded}`).catch(() => {});
  }
}

/**
 * Path al volumen bind-mounted del backend en el host. La suite asume
 * docker-compose dev con `./sistema-de-asientos-automatizado/libros`
 * mapeado a `/app/libros` (ver docker-compose.yml).
 *
 * Si el setup difiere, exportar `LIBROS_HOST_PATH` y se respetará.
 */
const LIBROS_HOST_PATH =
  process.env.LIBROS_HOST_PATH ??
  path.resolve(
    __dirname,
    "../../../sistema-de-asientos-automatizado/libros"
  );

/**
 * Vacía el contenido de un directorio sin borrar el directorio en sí
 * (el container lo tiene abierto via mount). Recursivo. Best-effort.
 */
async function purgeDirContents(dirPath: string): Promise<void> {
  let entries: import("node:fs").Dirent[];
  try {
    entries = await fs.readdir(dirPath, { withFileTypes: true });
  } catch {
    return;
  }
  for (const ent of entries) {
    const full = path.join(dirPath, ent.name);
    try {
      if (ent.isDirectory()) {
        await fs.rm(full, { recursive: true, force: true });
      } else {
        await fs.unlink(full);
      }
    } catch {
      // Si el OS bloquea por handles abiertos del container, seguimos.
    }
  }
}

/**
 * Limpieza dura: borra inbox de los 3 libros + reset pipeline + wipe del
 * filesystem para archivos en estado `confirmed`/`split` que la API se
 * niega a borrar.
 *
 * ⚠ Esta función ASUME que el backend tiene `libros/` bind-mounted en la
 * ruta dada por LIBROS_HOST_PATH. Si no, solo limpia via API y dejará
 * residuo confirmados (tests posteriores fallarán por duplicate).
 *
 * Llamar al inicio de cada test.
 */
export async function hardReset(api: APIRequestContext): Promise<void> {
  // 1. Cancelamos cualquier run en curso.
  await api.post(`/api/pipeline/cancel`).catch(() => {});
  await waitForPipelineIdle(api, { timeoutMs: 30_000 }).catch(() => {});

  // 2. API: borra inboxes (los que se dejen — confirmed/split los esquiva).
  for (const bid of ["gastos", "ingresos", "bienes"] as BookId[]) {
    await clearBookInbox(api, bid);
  }

  // 3. API: reset de asientos + estado runtime.
  const res = await api.post(`/api/pipeline/reset`);
  if (!res.ok() && res.status() !== 409) {
    throw new Error(`hardReset failed (reset): ${res.status()} ${await res.text()}`);
  }

  // 4. Filesystem nuke: archivos confirmed/split sobreviven a la API.
  //    Borramos:
  //      - libros/facturas/{compras,ventas,bienes}/  (inboxes)
  //      - libros/facturas/{compras,ventas,bienes}/_originales/ (archivados)
  //      - libros/asientos/                          (todas las carpetas asiento)
  //      - libros/.runtime/                          (lock + status)
  //    Sin esto, re-subir el mismo PDF entre tests da `duplicate`.
  for (const sub of ["compras", "ventas", "bienes"]) {
    await purgeDirContents(path.join(LIBROS_HOST_PATH, "facturas", sub));
  }
  await purgeDirContents(path.join(LIBROS_HOST_PATH, "asientos"));
  await purgeDirContents(path.join(LIBROS_HOST_PATH, ".runtime"));

  // 5. Forzamos invalidación de caches del backend con un reset extra.
  //    El backend mantiene cache de invoices/stats/clients que el reset
  //    via API limpia, pero el wipe FS no notifica. Otro reset bumpea
  //    los caches y deja el sistema consistente.
  await api.post(`/api/pipeline/reset`).catch(() => {});
}

/**
 * Lee /api/pipeline/status y devuelve { isRunning, startedAt, completedAt }.
 *
 * Contrato del backend (probado contra v actual):
 *   {
 *     "status": "running" | "completed" | (otros valores como "idle"),
 *     "started_at": ISO timestamp (puede persistir entre runs),
 *     "completed_at": ISO timestamp o null,
 *     "current_file": string | null,
 *     ...
 *   }
 *
 * Consideramos `isRunning = true` si:
 *   - status === "running", OR
 *   - hay started_at sin completed_at correspondiente.
 */
async function readPipelineStatus(
  api: APIRequestContext
): Promise<{ isRunning: boolean; startedAt: string | null; completedAt: string | null; raw: unknown }> {
  try {
    const res = await api.get(`/api/pipeline/status`);
    if (!res.ok()) return { isRunning: false, startedAt: null, completedAt: null, raw: null };
    const body = (await res.json()) as Record<string, unknown>;
    const status = String(body.status ?? "");
    const startedAt = (body.started_at as string | null) ?? null;
    const completedAt = (body.completed_at as string | null) ?? null;
    const isRunning =
      status === "running" || (startedAt != null && completedAt == null);
    return { isRunning, startedAt, completedAt, raw: body };
  } catch {
    return { isRunning: false, startedAt: null, completedAt: null, raw: null };
  }
}

/**
 * Polling de /api/pipeline/status hasta que el pipeline esté idle.
 * "Idle" = no running (status != running) y completed_at >= sentinel
 * si se pasa uno (para detectar transición de un run nuevo).
 */
export async function waitForPipelineIdle(
  api: APIRequestContext,
  opts: { timeoutMs?: number; pollMs?: number; sentinelStartedAt?: string | null } = {}
): Promise<void> {
  const timeoutMs = opts.timeoutMs ?? 5 * 60_000;
  const pollMs = opts.pollMs ?? 2_000;
  const sentinel = opts.sentinelStartedAt ?? null;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const { isRunning, startedAt, completedAt } = await readPipelineStatus(api);
    if (!isRunning) {
      // Si se pasó sentinel, exigimos que el run actual sea más nuevo que el
      // anterior — evita el caso de "completed" persistente del run previo.
      if (!sentinel) return;
      if (startedAt && startedAt > sentinel && completedAt) return;
      // Sin nuevo run detectado todavía — seguir polling
    }
    await new Promise((r) => setTimeout(r, pollMs));
  }
  throw new Error(`pipeline did not become idle within ${timeoutMs}ms`);
}

/**
 * Dispara POST /api/pipeline/run y espera al completed_at del NUEVO run.
 * Acepta 409 ("ya estaba corriendo") y procede al wait sin romper.
 */
export async function runPipelineAndWait(
  api: APIRequestContext,
  opts: { timeoutMs?: number } = {}
): Promise<void> {
  // Snapshot del started_at PREVIO para detectar transición real.
  const before = await readPipelineStatus(api);
  const sentinel = before.startedAt;

  const res = await api.post(`/api/pipeline/run`);
  if (!res.ok() && res.status() !== 409) {
    throw new Error(`runPipeline failed: ${res.status()} ${await res.text()}`);
  }

  // Da una pequeña gracia para que el thread background actualice started_at.
  await new Promise((r) => setTimeout(r, 2_000));

  await waitForPipelineIdle(api, {
    timeoutMs: opts.timeoutMs,
    sentinelStartedAt: sentinel,
  });
}

/**
 * Sube un archivo a un libro vía multipart.
 */
export async function uploadFile(
  api: APIRequestContext,
  bookId: BookId,
  filePath: string,
  filename: string
): Promise<{ status: number; body: unknown }> {
  const fs = await import("node:fs/promises");
  const buffer = await fs.readFile(filePath);
  const res = await api.post(`/api/books/${bookId}/upload`, {
    multipart: {
      files: { name: filename, mimeType: "application/pdf", buffer },
    },
  });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = await res.text();
  }
  return { status: res.status(), body };
}

export interface InvoiceListItem {
  id: string;
  folder_name?: string;
  libro?: string | null;
  status?: string | null;
  decision_global: string;
  timestamp: string;
  nif_entidad: string;
  nombre_entidad: string;
  numero_factura: string;
  total_euros: number;
  duplicate_of?: string;
}

/**
 * Fetch /api/invoices con query params. include_confirmed=true por defecto
 * en esta suite — necesitamos verificar el estado post-confirm.
 */
export async function fetchInvoices(
  api: APIRequestContext,
  includeDone = true,
  includeConfirmed = true
): Promise<InvoiceListItem[]> {
  const params = new URLSearchParams();
  if (!includeDone) params.set("include_done", "false");
  if (includeConfirmed) params.set("include_confirmed", "true");
  const qs = params.toString() ? `?${params.toString()}` : "";
  const res = await api.get(`/api/invoices${qs}`);
  if (!res.ok()) throw new Error(`fetchInvoices failed: ${res.status()}`);
  const body = await res.json();
  return body.invoices ?? [];
}

/**
 * Filtra invoices que vienen del PDF multi-factura (doc_id empieza por el
 * stem del fichero subido).
 */
export function invoicesFromMultiPdf(
  invoices: InvoiceListItem[]
): InvoiceListItem[] {
  const stem = MULTI_INVOICE_FILENAME.replace(/\.pdf$/i, "");
  return invoices.filter((inv) => inv.id.startsWith(stem));
}

/**
 * Snapshot del `/api/pipeline/batch` actual. Útil para artefactos
 * diagnósticos durante un run en curso.
 */
export async function fetchBatch(api: APIRequestContext): Promise<unknown> {
  const res = await api.get(`/api/pipeline/batch`);
  if (!res.ok()) return { error: `batch endpoint returned ${res.status()}` };
  return res.json();
}

/**
 * Adjunta un payload JSON al test report. Wrapper para conveniencia.
 */
export async function attachJson(
  testInfo: TestInfo,
  name: string,
  data: unknown
): Promise<void> {
  await testInfo.attach(name, {
    body: JSON.stringify(data, null, 2),
    contentType: "application/json",
  });
}

/**
 * Sube el PDF multi-factura via UI (drag-drop sobre el input file del libro).
 * Asume que la app está en stage="books".
 */
export async function uploadMultiPdfViaUI(
  page: Page,
  bookId: BookId
): Promise<void> {
  // Localiza la card del libro por su título (h3). El input[type=file] vive
  // anidado dentro; tras el primer upload sigue ahí (uno por card).
  const card = page.locator("div.border", { has: page.locator("h3", { hasText: BOOK_LABELS[bookId] }) }).first();
  await expect(card).toBeVisible();
  // El input está oculto (className=hidden). setInputFiles funciona igualmente.
  const input = card.locator('input[type="file"]').first();
  await input.setInputFiles(MULTI_INVOICE_PDF);
  // Esperar a que aparezca el fichero en la lista visible del card o que
  // desaparezca el spinner "Subiendo…". Damos 60s para servidor lento.
  await expect(card.getByText(MULTI_INVOICE_FILENAME)).toBeVisible({ timeout: 60_000 });
}

/**
 * Click en "Escanear N Factura(s) →" en el footer de BooksManager.
 */
export async function clickScanFooterButton(page: Page): Promise<void> {
  await page.getByRole("button", { name: /escanear .* factura/i }).click();
}

/**
 * Tras subir un PDF multi-factura por UI, la app debe transitar al stage
 * "Pre-revisión" automáticamente (callback onUploadComplete en books-manager).
 * Este helper espera el heading "Pre-revisión", verifica el detected count
 * y pulsa "Escanear N Factura(s)" para avanzar a processing → review.
 *
 * Reemplaza la asunción vieja "navigateToReview salta desde books" que asumían
 * los specs 02/03/07 antes del fix de Pre-revisión Fase 1.
 *
 * Spec: docs/superpowers/specs/2026-05-28-pre-revision-callback-y-test-drift-design.md
 */
export async function expectPreRevisionAndConfirm(
  page: Page,
  { expectedInvoices }: { expectedInvoices: number }
): Promise<void> {
  const preRevisionHeader = page.getByRole("heading", { name: /Pre-revisión/i });
  await expect(preRevisionHeader).toBeVisible({ timeout: 90_000 });

  const escanearBtn = page.getByRole("button", { name: /Escanear .* factura/i });
  await expect(escanearBtn).toBeVisible();
  await expect(escanearBtn).toBeEnabled();

  const label = (await escanearBtn.textContent())?.trim() ?? "";
  const match = label.match(/Escanear\s+(\d+)/i);
  // Fail loud si el formato del botón cambió, no degradar a "0 < N".
  expect(match, `formato del botón Escanear cambió: "${label}"`).not.toBeNull();
  const detected = parseInt(match![1], 10);
  expect(detected).toBeGreaterThanOrEqual(expectedInvoices);

  await escanearBtn.click();

  // Esperar el reviewer (botón "Aprobar" en footer cuando carga la 1ª factura).
  const aprobar = page.getByRole("button", { name: /^(Aprobar|Aprobada)$/i });
  await expect(aprobar).toBeVisible({ timeout: 5 * 60_000 });
}

export const test = base.extend({});
export { expect };
