"use client";

/**
 * Stage intermedio entre `Gestión` (upload) y `Escaneando` (pipeline OCR).
 *
 * Tras subir archivos, el backend ejecuta el splitter Fase 1 (Gemini Vision)
 * de forma síncrona y devuelve `pre_scan` por archivo con el desglose real
 * de facturas detectadas. Este componente le presenta el desglose al
 * operario antes de pulsar "Escanear", para que:
 *
 *  - vea inmediatamente el número correcto de facturas (no de archivos);
 *  - desbloquee archivos en `pre_scan_failed` (retry o override-as-single);
 *  - confirme el lote o vuelva a Gestión para añadir más PDFs.
 */

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  FileText,
  Files,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  ShieldQuestion,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  deleteBookFile,
  overridePreScan,
  retryPreScan,
  runPipeline,
} from "@/lib/api";
import type { BookFile, Libro, PreScanResult } from "@/lib/types";

const BOOK_LABELS: Record<Libro, string> = {
  gastos: "Compras y Gastos",
  ingresos: "Ventas e Ingresos",
  bienes: "Bienes de Inversión",
};

/** Snapshot inmutable de un upload concreto que llegó al pre-review. */
export interface PreReviewUploadEntry {
  bookId: Libro;
  files: BookFile[];
}

interface PreReviewStageProps {
  /** Resultados de los uploads recientes que activaron este stage. */
  entries: PreReviewUploadEntry[];
  /** Actualiza el estado de un archivo concreto (tras retry/override). */
  onFileUpdated: (
    bookId: Libro,
    fileName: string,
    patch: Partial<BookFile>,
  ) => void;
  /** Quita un archivo del snapshot (tras delete). */
  onFileRemoved: (bookId: Libro, fileName: string) => void;
  /** Vuelve a Gestión para añadir más archivos. */
  onBack: () => void;
  /** Lanza el pipeline OCR y transiciona a "processing". */
  onPipelineStart: () => void;
}

type FileAction = "retry" | "override" | "delete" | null;

function summarize(entries: PreReviewUploadEntry[]) {
  let totalInvoices = 0;
  let filesBlocked = 0;
  let filesOk = 0;
  let filesSkipped = 0;
  for (const entry of entries) {
    for (const f of entry.files) {
      const pre = f.pre_scan;
      if (!pre) continue;
      if (pre.status === "failed") filesBlocked += 1;
      else if (pre.status === "skipped_non_pdf") {
        filesSkipped += 1;
        totalInvoices += pre.detected_invoices;
      } else if (pre.status === "skipped_disabled") {
        filesSkipped += 1;
        totalInvoices += pre.detected_invoices;
      } else {
        filesOk += 1;
        totalInvoices += pre.detected_invoices;
      }
    }
  }
  return { totalInvoices, filesBlocked, filesOk, filesSkipped };
}

function StatusBadge({ pre }: { pre: PreScanResult | null | undefined }) {
  if (!pre) {
    return (
      <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-slate-50 text-slate-500 border-slate-200">
        sin análisis
      </span>
    );
  }
  if (pre.status === "failed") {
    return (
      <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-red-50 text-red-700 border-red-200">
        bloqueado
      </span>
    );
  }
  if (pre.status === "split") {
    return (
      <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-violet-50 text-violet-700 border-violet-200">
        {pre.detected_invoices} facturas
      </span>
    );
  }
  if (pre.status === "skipped_non_pdf") {
    return (
      <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-slate-50 text-slate-600 border-slate-200">
        imagen
      </span>
    );
  }
  if (pre.status === "skipped_disabled") {
    return (
      <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-amber-50 text-amber-700 border-amber-200">
        sin pre-scan
      </span>
    );
  }
  return (
    <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border bg-emerald-50 text-emerald-700 border-emerald-200">
      1 factura
    </span>
  );
}

function PreScanFileRow({
  bookId,
  file,
  action,
  onAction,
  onFileUpdated,
  onFileRemoved,
}: {
  bookId: Libro;
  file: BookFile;
  action: FileAction;
  onAction: (key: string, action: FileAction) => void;
  onFileUpdated: PreReviewStageProps["onFileUpdated"];
  onFileRemoved: PreReviewStageProps["onFileRemoved"];
}) {
  const pre = file.pre_scan;
  const docId = file.name.replace(/\.[^.]+$/, "");
  const fileKey = `${bookId}/${file.name}`;

  const handleRetry = async () => {
    onAction(fileKey, "retry");
    try {
      const result = await retryPreScan(bookId, docId);
      onFileUpdated(bookId, file.name, {
        pre_scan: result.pre_scan,
        status:
          result.pre_scan.status === "failed"
            ? "pre_scan_failed"
            : result.pre_scan.status === "split"
            ? "split"
            : "uploaded",
      });
      if (result.pre_scan.status === "failed") {
        toast.error(`${file.name}: el pre-scan volvió a fallar.`);
      } else if (result.pre_scan.status === "split") {
        toast.success(
          `${file.name}: ${result.pre_scan.detected_invoices} facturas detectadas.`,
        );
      } else {
        toast.success(`${file.name}: detectada 1 factura.`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Error reintentando: ${msg}`);
    } finally {
      onAction(fileKey, null);
    }
  };

  const handleOverride = async () => {
    if (!window.confirm(
      `¿Marcar "${file.name}" como factura única? El pipeline lo procesará como 1 factura sin volver a llamar al splitter.`,
    )) {
      return;
    }
    onAction(fileKey, "override");
    try {
      await overridePreScan(bookId, docId, { asSingle: true });
      onFileUpdated(bookId, file.name, {
        status: "uploaded",
        pre_scan: {
          status: "single",
          n_pages: pre?.n_pages ?? 0,
          detected_invoices: 1,
          children: [],
          error: null,
        },
      });
      toast.success(`${file.name}: forzado como 1 factura.`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Error overrideando: ${msg}`);
    } finally {
      onAction(fileKey, null);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(`¿Eliminar "${file.name}" del lote?`)) return;
    onAction(fileKey, "delete");
    try {
      await deleteBookFile(bookId, file.name);
      onFileRemoved(bookId, file.name);
      toast.success(`Archivo eliminado: ${file.name}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`Error eliminando: ${msg}`);
    } finally {
      onAction(fileKey, null);
    }
  };

  const isPdf = file.name.toLowerCase().endsWith(".pdf");
  const blocked = pre?.status === "failed";
  const busy = action !== null;

  return (
    <div
      className={cn(
        "border rounded-lg p-3 transition-colors",
        blocked
          ? "border-red-200 bg-red-50/40"
          : pre?.status === "split"
          ? "border-violet-200 bg-violet-50/40"
          : "border-slate-200 bg-white",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="flex-shrink-0 mt-0.5">
          {isPdf ? (
            pre?.status === "split" ? (
              <Files className="h-4 w-4 text-violet-600" />
            ) : blocked ? (
              <AlertCircle className="h-4 w-4 text-red-500" />
            ) : (
              <FileText className="h-4 w-4 text-slate-500" />
            )
          ) : (
            <ImageIcon className="h-4 w-4 text-slate-400" />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-xs font-mono truncate">{file.name}</p>
            <StatusBadge pre={pre} />
          </div>
          <p className="text-[10px] text-slate-400 mt-0.5">
            {file.size_kb} KB
            {pre && pre.n_pages > 0 ? ` · ${pre.n_pages} página${pre.n_pages !== 1 ? "s" : ""}` : ""}
          </p>

          {pre?.status === "split" && pre.children.length > 0 && (
            <ul className="mt-2 space-y-0.5">
              {pre.children.map((child) => (
                <li
                  key={child.doc_id}
                  className="text-[10px] font-mono text-violet-700 flex items-center gap-2"
                >
                  <span className="text-violet-400">↳</span>
                  <span className="truncate">{child.doc_id}</span>
                  {child.pages && child.pages.length > 0 && (
                    <span className="text-violet-400">
                      p.{child.pages.join("-")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {blocked && pre.error && (
            <div className="mt-2 text-[11px] text-red-600 bg-red-50 border border-red-100 rounded px-2 py-1.5">
              <strong>{pre.error.kind}</strong>: {pre.error.message}
              {pre.error.attempts > 0 && ` (tras ${pre.error.attempts} intento${pre.error.attempts !== 1 ? "s" : ""})`}
            </div>
          )}
        </div>
      </div>

      {blocked && (
        <div className="flex flex-wrap gap-2 mt-3 ml-7">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={handleRetry}
            className="h-7 text-[10px] uppercase tracking-wider rounded-none"
          >
            {action === "retry" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3 mr-1" />
            )}
            Reintentar análisis
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={handleOverride}
            className="h-7 text-[10px] uppercase tracking-wider rounded-none"
          >
            {action === "override" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <ShieldQuestion className="h-3 w-3 mr-1" />
            )}
            Tratar como 1 factura
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={handleDelete}
            className="h-7 text-[10px] uppercase tracking-wider text-red-600 hover:text-red-800 hover:bg-red-50 rounded-none"
          >
            {action === "delete" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Trash2 className="h-3 w-3 mr-1" />
            )}
            Eliminar
          </Button>
        </div>
      )}
    </div>
  );
}

export function PreReviewStage({
  entries,
  onFileUpdated,
  onFileRemoved,
  onBack,
  onPipelineStart,
}: PreReviewStageProps) {
  const [pendingActions, setPendingActions] = useState<Record<string, FileAction>>({});
  const [launching, setLaunching] = useState(false);
  const [forceContinue, setForceContinue] = useState(false);

  const summary = useMemo(() => summarize(entries), [entries]);
  const hasBlocked = summary.filesBlocked > 0;

  const setAction = (key: string, action: FileAction) => {
    setPendingActions((prev) => {
      const next = { ...prev };
      if (action === null) delete next[key];
      else next[key] = action;
      return next;
    });
  };

  const handleLaunch = async () => {
    if (hasBlocked && !forceContinue) {
      toast.warning(
        "Hay archivos bloqueados. Resuélvelos o continúa ignorándolos antes de escanear.",
      );
      return;
    }
    setLaunching(true);
    try {
      const result = await runPipeline();
      toast.success(`Pipeline iniciado: ${result.total} archivo(s) a procesar`);
      onPipelineStart();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (message.includes("PIPELINE_ALREADY_RUNNING")) {
        toast.warning("El pipeline ya está en ejecución");
        onPipelineStart();
      } else {
        toast.error(`Error iniciando pipeline: ${message}`);
      }
      setLaunching(false);
    }
  };

  if (entries.length === 0) {
    // Defensa: si el caller nos abre sin datos, redirigir a books.
    return (
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <p className="text-sm text-slate-600 mb-4">
            No hay archivos pendientes de pre-revisión.
          </p>
          <Button onClick={onBack} variant="outline" className="text-xs rounded-none uppercase tracking-[0.15em]">
            Volver a Gestión
          </Button>
        </div>
      </div>
    );
  }

  const effectiveTotal = forceContinue
    ? summary.totalInvoices
    : hasBlocked
    ? 0
    : summary.totalInvoices;

  return (
    <div className="flex flex-col h-full bg-white">
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0">
        <div>
          <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">
            Pre-revisión
          </h2>
          <p className="text-[10px] text-slate-400 mt-0.5 font-mono">
            Resultado del análisis Fase 1 (Gemini Vision) — verifica el desglose antes de escanear.
          </p>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono">
          {summary.filesOk > 0 && (
            <span className="text-emerald-600">
              <CheckCircle2 className="h-3 w-3 inline mr-1" />
              {summary.filesOk} OK
            </span>
          )}
          {summary.filesBlocked > 0 && (
            <span className="text-red-600">
              <AlertTriangle className="h-3 w-3 inline mr-1" />
              {summary.filesBlocked} bloqueado{summary.filesBlocked !== 1 ? "s" : ""}
            </span>
          )}
          {summary.filesSkipped > 0 && (
            <span className="text-slate-500">
              {summary.filesSkipped} sin análisis
            </span>
          )}
        </div>
      </header>

      <ScrollArea className="flex-1">
        <div className="p-6 space-y-6 max-w-3xl mx-auto">
          <div className="bg-slate-50 border border-slate-200 rounded-lg p-4">
            <p className="text-xs text-slate-600">
              Se han detectado{" "}
              <strong className="text-slate-900 text-base">
                {summary.totalInvoices} factura{summary.totalInvoices !== 1 ? "s" : ""}
              </strong>{" "}
              en{" "}
              <strong>
                {entries.reduce((n, e) => n + e.files.length, 0)} archivo(s)
              </strong>
              {summary.filesBlocked > 0 && (
                <>
                  {" "}— <strong className="text-red-700">{summary.filesBlocked} bloqueado(s)</strong>{" "}
                  por error en el análisis automático.
                </>
              )}
            </p>
          </div>

          {entries.map((entry) => (
            <section key={entry.bookId} className="space-y-2">
              <h3 className="text-[10px] font-black uppercase tracking-[0.15em] text-slate-500">
                {BOOK_LABELS[entry.bookId]}
              </h3>
              <div className="space-y-2">
                {entry.files.map((file) => (
                  <PreScanFileRow
                    key={`${entry.bookId}/${file.name}`}
                    bookId={entry.bookId}
                    file={file}
                    action={pendingActions[`${entry.bookId}/${file.name}`] ?? null}
                    onAction={setAction}
                    onFileUpdated={onFileUpdated}
                    onFileRemoved={onFileRemoved}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      </ScrollArea>

      <footer className="h-24 flex items-center justify-center gap-4 px-8 bg-white border-t flex-shrink-0">
        <Button
          variant="outline"
          size="lg"
          onClick={onBack}
          className="h-11 text-[10px] uppercase tracking-[0.2em] rounded-none"
        >
          ← Volver a Gestión
        </Button>
        {hasBlocked && !forceContinue && (
          <Button
            variant="ghost"
            size="lg"
            onClick={() => setForceContinue(true)}
            className="h-11 text-[10px] uppercase tracking-[0.2em] text-amber-700 hover:text-amber-900 hover:bg-amber-50 rounded-none"
          >
            Continuar ignorando bloqueados ({summary.totalInvoices})
          </Button>
        )}
        <Button
          size="lg"
          onClick={handleLaunch}
          disabled={effectiveTotal === 0 || launching}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            (effectiveTotal === 0 || launching) && "opacity-50 cursor-not-allowed",
          )}
        >
          {launching ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Iniciando…
            </>
          ) : effectiveTotal === 0 ? (
            "Resuelve los bloqueos"
          ) : (
            <>Escanear {effectiveTotal} Factura(s) →</>
          )}
        </Button>
      </footer>
    </div>
  );
}
