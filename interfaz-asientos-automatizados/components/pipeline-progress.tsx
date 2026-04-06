"use client";

import { useState, useEffect } from "react";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
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

  // Poll with recursive setTimeout (only one request in flight at a time)
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

        if (result.status === "completed") {
          completionTimer = setTimeout(() => {
            if (!cancelled) onComplete();
          }, 800);
          return; // Stop polling
        } else if (result.status === "error") {
          setError(result.error_message || "Error desconocido en el pipeline");
          return; // Stop polling on error
        } else if (result.status === "idle") {
          // Only transition to review if we previously saw "running"
          if (status?.status === "running" || status?.started_at) {
            onComplete();
          }
          return;
        }

        // Schedule next poll
        if (!cancelled) {
          setTimeout(poll, 2000);
        }
      } catch (err) {
        console.error("Error polling pipeline status:", err);
        if (!cancelled) {
          setTimeout(poll, 2000);
        }
      }
    };

    // Initial poll
    poll().then(() => {
      if (!cancelled) setLoading(false);
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
      if (completionTimer) clearTimeout(completionTimer);
    };
  }, [onComplete, onBack]);

  const progress = status && status.total > 0
    ? Math.round((status.processed / status.total) * 100)
    : 0;

  // Loading state
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

  // Error state (pipeline error, not network error)
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
        {/* Title */}
        <h2 className="text-xs font-black uppercase tracking-[0.15em] text-slate-800 mb-8">
          Asientos Automatizados
        </h2>

        {/* Status badge */}
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

        {/* Progress bar */}
        <div className="mb-4">
          {status?.total === 0 ? (
            /* Indeterminate progress — animate bar */
            <div className="relative">
              <Progress value={0} className="h-2 bg-slate-100" />
              <style>{`
                @keyframes indeterminate {
                  0% { transform: translateX(-100%); }
                  50% { transform: translateX(100%); }
                  100% { transform: translateX(-100%); }
                }
                .animate-indeterminate::after {
                  content: '';
                  position: absolute;
                  top: 0;
                  left: 0;
                  right: 0;
                  bottom: 0;
                  background: linear-gradient(90deg, transparent, hsl(173 80% 40%), transparent);
                  animation: indeterminate 1.5s ease-in-out infinite;
                }
              `}</style>
            </div>
          ) : (
            <Progress value={progress} className="h-2 bg-slate-100" />
          )}
        </div>

        {/* Progress text */}
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

        {/* Back button (always available) */}
        <div className="mt-8">
          <Button
            onClick={onBack}
            variant="ghost"
            className="text-[10px] uppercase tracking-[0.15em] text-slate-400 hover:text-slate-600"
          >
            Volver a Gestión
          </Button>
        </div>
      </div>
    </div>
  );
}
