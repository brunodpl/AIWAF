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
import { Loader2, CheckCircle2, XCircle } from "lucide-react";
import { fetchPipelineStatus } from "@/lib/api";
import type { PipelineStatus } from "@/lib/types";

interface PipelineProgressProps {
  onComplete: () => void;
  onBack: () => void;
}

export function PipelineProgress({ onComplete, onBack }: PipelineProgressProps) {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Use ref to always access latest status inside the polling closure (avoids stale closure bug)
  const statusRef = useRef<PipelineStatus | null>(null);
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

        statusRef.current = result;
        setStatus(result);
        setError(null);
        setLoading(false);

        if (result.status === "completed") {
          completionTimer = setTimeout(() => {
            if (!cancelled) onCompleteRef.current();
          }, 800);
          return;
        }

        if (result.status === "error") {
          setError(result.error_message || "Error desconocido en el pipeline");
          return;
        }

        if (result.status === "idle") {
          // Only transition if we have seen a previous running/started state
          const prev = statusRef.current;
          if (prev?.status === "running" || prev?.started_at) {
            if (!cancelled) onCompleteRef.current();
          }
          return;
        }

        if (!cancelled) {
          setTimeout(poll, 2000);
        }
      } catch {
        if (!cancelled) {
          setTimeout(poll, 2000);
        }
      }
    };

    poll();

    return () => {
      cancelled = true;
      if (completionTimer) clearTimeout(completionTimer);
    };
  }, []); // stable: uses refs for callbacks, no deps needed

  const progress =
    status && status.total > 0
      ? Math.round((status.processed / status.total) * 100)
      : 0;

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
          <Button
            onClick={onBack}
            variant="outline"
            className="text-xs rounded-none uppercase tracking-[0.15em]"
          >
            Volver a Gestión
          </Button>
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
          {isRunning && (
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

        {/* Progress bar */}
        <div className="mb-4 relative overflow-hidden rounded">
          {status?.total === 0 || !status?.total ? (
            <div className="relative h-2 bg-slate-100 overflow-hidden rounded">
              <div
                className="absolute inset-y-0 w-1/3 bg-teal-500/60 rounded"
                style={{
                  animation: "indeterminate-slide 1.5s ease-in-out infinite",
                }}
              />
              <style>{`
                @keyframes indeterminate-slide {
                  0% { left: -33%; }
                  100% { left: 100%; }
                }
              `}</style>
            </div>
          ) : (
            <Progress value={progress} className="h-2 bg-slate-100" />
          )}
        </div>

        {status && (
          <div className="space-y-2">
            <p className="text-sm text-slate-500">
              {status.current_file
                ? `Procesando ${status.current_file}...`
                : "Procesando facturas..."}
              {status.total > 0 && ` (${status.processed}/${status.total})`}
            </p>
            {status.total > 0 && (
              <p className="text-xs font-mono text-slate-300">{progress}% completado</p>
            )}
          </div>
        )}

        {/* Back button: warn if pipeline is still running */}
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
                    El pipeline sigue procesando facturas en segundo plano. Si vuelves ahora,
                    podrás retomar la revisión cuando termine. ¿Confirmas que quieres salir?
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancelar</AlertDialogCancel>
                  <AlertDialogAction onClick={onBack}>Volver de todas formas</AlertDialogAction>
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
