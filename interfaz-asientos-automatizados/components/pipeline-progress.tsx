"use client";

import { useState, useEffect, useRef } from "react";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import { Loader2, CheckCircle2, XCircle, RotateCcw } from "lucide-react";
import { fetchPipelineStatus, runPipeline } from "@/lib/api";
import { toast } from "sonner";
import type { PipelineStatus } from "@/lib/types";

interface PipelineProgressProps {
  onComplete: () => void;
  onBack: () => void;
  /** Saltar a la pantalla de revisión sin esperar a que termine el pipeline */
  onJumpToReview?: (totalQueued: number) => void;
}

export function PipelineProgress({ onComplete, onBack, onJumpToReview }: PipelineProgressProps) {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
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

  // FIX #2: onComplete y onBack via refs para no disparar re-mount del efecto de polling
  const onCompleteRef = useRef(onComplete);
  const onBackRef = useRef(onBack);
  useEffect(() => { onCompleteRef.current = onComplete; }, [onComplete]);
  useEffect(() => { onBackRef.current = onBack; }, [onBack]);

  useEffect(() => {
    let cancelled = false;
    let completionTimer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      if (cancelled) return;
      try {
        const result = await fetchPipelineStatus();
        if (cancelled) return;
        setStatus(result);
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
  const progress = status && status.total > 0
    ? Math.round((status.processed / status.total) * 100)
    : 0;

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
            <Button
              onClick={onBack}
              variant="outline"
              className="text-xs rounded-none uppercase tracking-[0.15em]"
            >
              Volver a Gestión
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center bg-slate-50">
      <div className="text-center max-w-md w-full px-8">
        <h2 className="text-xs font-black uppercase tracking-[0.15em] text-slate-800 mb-8">
          Asientos Automatizados
        </h2>

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

        {/* FIX #8: progress bar indeterminada — wrapper con overflow-hidden y position relative */}
        <div className="mb-4">
          {status?.total === 0 ? (
            <div className="relative h-2 w-full bg-slate-100 overflow-hidden rounded-full">
              <style>{`
                @keyframes indeterminate {
                  0%   { transform: translateX(-100%); }
                  50%  { transform: translateX(100%); }
                  100% { transform: translateX(-100%); }
                }
                .bar-indeterminate {
                  position: absolute;
                  top: 0; left: 0; right: 0; bottom: 0;
                  background: linear-gradient(90deg, transparent, hsl(173 80% 40%), transparent);
                  animation: indeterminate 1.5s ease-in-out infinite;
                }
              `}</style>
              <div className="bar-indeterminate" />
            </div>
          ) : (
            <Progress value={progress} className="h-2 bg-slate-100" />
          )}
        </div>

        {status && (
          <div className="space-y-2">
            <p className="text-sm text-slate-500">
              Procesando {status.current_file || "facturas"}... ({status.processed}/{status.total})
            </p>
            {status.total > 0 && (
              <p className="text-xs font-mono text-slate-300">
                {progress}% completado
              </p>
            )}
          </div>
        )}

        {isRunning && onJumpToReview && status && status.processed >= 1 && (
          <div className="mt-6">
            <Button
              onClick={() => onJumpToReview(totalQueuedRef.current)}
              variant="default"
              className="text-xs rounded-none uppercase tracking-[0.15em] bg-teal-600 hover:bg-teal-700"
            >
              Ver {status.processed} factura{status.processed === 1 ? "" : "s"} ya procesada{status.processed === 1 ? "" : "s"}
            </Button>
            <p className="text-[10px] text-slate-400 mt-2">
              El procesamiento continúa en segundo plano. Las nuevas facturas se irán añadiendo a la lista de revisión.
            </p>
          </div>
        )}

        {/* FIX #11: AlertDialog de confirmación al volver si el pipeline está running */}
        <div className="mt-8">
          {isRunning ? (
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  variant="ghost"
                  className="text-[10px] uppercase tracking-[0.15em] text-slate-400 hover:text-slate-600"
                >
                  Volver a Gestión
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Pipeline en ejecución</AlertDialogTitle>
                  <AlertDialogDescription>
                    El pipeline está procesando facturas en segundo plano. Si vuelves ahora,
                    el procesamiento continuará pero no verás el progreso en tiempo real.
                    ¿Seguro que quieres volver?
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Seguir esperando</AlertDialogCancel>
                  <AlertDialogAction onClick={onBack}>Volver igualmente</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          ) : (
            <Button
              onClick={onBack}
              variant="ghost"
              className="text-[10px] uppercase tracking-[0.15em] text-slate-400 hover:text-slate-600"
            >
              Volver a Gestión
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
