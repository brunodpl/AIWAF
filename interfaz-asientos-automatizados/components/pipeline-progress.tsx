"use client";

import { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Loader2, CheckCircle2, XCircle, RotateCcw, ChevronRight } from "lucide-react";
import { fetchPipelineBatch, fetchPipelineStatus, runPipeline } from "@/lib/api";
import { toast } from "sonner";
import type { PipelineBatch, PipelineStatus } from "@/lib/types";
import { BatchOverview } from "@/components/batch-overview";

interface PipelineProgressProps {
  onComplete: () => void;
  /** Saltar a la pantalla de revisión sin esperar a que termine el pipeline */
  onJumpToReview?: (totalQueued: number) => void;
}

export function PipelineProgress({ onComplete, onJumpToReview }: PipelineProgressProps) {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [batch, setBatch] = useState<PipelineBatch | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [retryCount, setRetryCount] = useState(0);
  const statusRef = useRef<PipelineStatus | null>(null);
  const totalQueuedRef = useRef<number>(0);
  const hasSeenRunning = useRef(false);
  const errorCountRef = useRef(0);

  useEffect(() => {
    statusRef.current = status;
    if (status?.status === "running") {
      hasSeenRunning.current = true;
    }
  }, [status]);

  // onComplete via ref para no disparar re-mount del efecto de polling.
  const onCompleteRef = useRef(onComplete);
  useEffect(() => { onCompleteRef.current = onComplete; }, [onComplete]);

  useEffect(() => {
    let cancelled = false;
    let completionTimer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (cancelled) return;
      try {
        const [result, batchResult] = await Promise.all([
          fetchPipelineStatus(),
          // Vista en vivo del lote — si falla, no rompe el polling de status.
          fetchPipelineBatch().catch(() => null),
        ]);
        if (cancelled) return;
        setStatus(result);
        if (batchResult) setBatch(batchResult);
        setError(null);
        errorCountRef.current = 0; // Reset backoff on success
        if (result.total > 0) totalQueuedRef.current = result.total;

        if (result.status === "completed") {
          completionTimer = setTimeout(() => {
            if (!cancelled) onCompleteRef.current();
          }, 800);
          return;
        } else if (result.status === "error") {
          setError(result.error_message || "Error desconocido en el pipeline");
          return;
        } else if (result.status === "idle") {
          // Solo completar si ya vimos "running" — evita falso positivo con status stale
          if (hasSeenRunning.current) {
            onCompleteRef.current();
            return;
          }
          // Si no hemos visto running, seguir polling (pipeline puede no haber arrancado aún)
          if (!cancelled) {
            setTimeout(poll, 2000);
          }
          return;
        }

        if (!cancelled) {
          setTimeout(poll, 2000);
        }
      } catch (err) {
        console.error("Error polling pipeline status:", err);
        if (!cancelled) {
          // Backoff exponencial: 2s → 3s → 4.5s → ... max 15s
          errorCountRef.current += 1;
          const delay = Math.min(2000 * Math.pow(1.5, errorCountRef.current - 1), 15000);
          setTimeout(poll, delay);
        }
      }
    };

    poll().then(() => {
      if (!cancelled) setLoading(false);
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
      if (completionTimer) clearTimeout(completionTimer);
    };
  // retryCount en deps permite re-triggerar el polling al reintentar
  }, [retryCount]);

  const isRunning = status?.status === "running";

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
          <p className="text-sm text-slate-500">Conectando con el pipeline...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-50">
        <div className="text-center max-w-md p-8">
          <XCircle className="h-12 w-12 mx-auto mb-4 text-red-300" />
          <p className="text-sm text-slate-600 mb-2 font-bold">Error en el pipeline</p>
          <p className="text-xs text-slate-400 font-mono mb-6">{error}</p>
          <div className="flex gap-3 justify-center">
            <Button
              onClick={async () => {
                try {
                  setError(null);
                  setLoading(true);
                  await runPipeline();
                  hasSeenRunning.current = false;
                  errorCountRef.current = 0;
                  setRetryCount(c => c + 1);
                  toast.success("Pipeline reiniciado");
                } catch (err) {
                  setError(err instanceof Error ? err.message : "Error reiniciando pipeline");
                  setLoading(false);
                }
              }}
              variant="default"
              className="text-xs rounded-none uppercase tracking-[0.15em]"
            >
              <RotateCcw className="h-3 w-3 mr-1" />
              Reintentar
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center bg-slate-50">
      <div className="text-center max-w-md w-full px-8">
        <h2 className="text-xs font-black uppercase tracking-[0.15em] text-slate-800 mb-2">
          Escaneando facturas
        </h2>
        <p className="text-[10px] text-slate-400 font-mono mb-6">
          {status?.status === "running"
            ? "Procesamiento OCR en curso — puedes empezar a revisar cuando esté la primera factura."
            : status?.status === "completed"
            ? "Escaneo completado — pulsa 'Empezar a revisar' para continuar."
            : "Conectando con el pipeline..."}
        </p>

        <div className="flex justify-center mb-6">
          {status?.status === "running" && (
            <span className="inline-flex items-center gap-2 text-xs font-bold text-teal-600 bg-teal-50 px-3 py-1 rounded-full">
              <Loader2 className="h-3 w-3 animate-spin" />
              Procesando...
            </span>
          )}
          {status?.status === "completed" && (
            <span className="inline-flex items-center gap-2 text-xs font-bold text-emerald-600 bg-emerald-50 px-3 py-1 rounded-full">
              <CheckCircle2 className="h-3 w-3" />
              Completado
            </span>
          )}
        </div>

        <BatchOverview batch={batch} />

        {isRunning && onJumpToReview && status && status.processed >= 1 && (
          <div className="mt-8">
            <Button
              onClick={() => onJumpToReview(totalQueuedRef.current)}
              size="lg"
              className="h-12 px-8 bg-teal-600 hover:bg-teal-700 text-white font-bold uppercase text-xs tracking-[0.2em] rounded-none shadow-lg"
            >
              Empezar a revisar ({status.processed} factura{status.processed === 1 ? "" : "s"} lista{status.processed === 1 ? "" : "s"})
              <ChevronRight className="h-4 w-4 ml-2" />
            </Button>
            <p className="text-[10px] text-slate-400 mt-3">
              El escaneo continúa en segundo plano. Las nuevas facturas se irán activando en el reviewer.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
