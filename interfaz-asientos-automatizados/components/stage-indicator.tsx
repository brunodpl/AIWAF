"use client";

import { cn } from "@/lib/utils";
import { Check, RotateCcw, Loader2 } from "lucide-react";
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

type Stage = "books" | "processing" | "review" | "export";

const STAGES: { key: Stage; label: string }[] = [
  { key: "books", label: "Gestión" },
  { key: "processing", label: "Escaneando" },
  { key: "review", label: "Revisión" },
  { key: "export", label: "Exportar" },
];

interface StageIndicatorProps {
  stage: Stage;
  className?: string;
  onStageClick?: (stage: Stage) => void;
  onReset?: () => void;
  resetting?: boolean;
}

export function StageIndicator({ stage, className, onStageClick, onReset, resetting }: StageIndicatorProps) {
  const currentIndex = STAGES.findIndex((s) => s.key === stage);

  return (
    <div className={cn("flex items-center justify-between gap-2 text-xs", className)}>
      <div className="flex items-center gap-2">
        {STAGES.map((s, idx) => {
          const isCompleted = idx < currentIndex;
          const isActive = idx === currentIndex;
          const isFuture = idx > currentIndex;
          const isClickable = isCompleted && !!onStageClick;

          return (
            <div key={s.key} className="flex items-center gap-2">
              <div
                className={cn(
                  "flex items-center gap-1.5",
                  isClickable && "cursor-pointer group/step"
                )}
                onClick={isClickable ? () => onStageClick(s.key) : undefined}
                role={isClickable ? "button" : undefined}
                aria-label={isClickable ? `Volver a ${s.label}` : undefined}
                tabIndex={isClickable ? 0 : undefined}
                onKeyDown={isClickable ? (e) => { if (e.key === "Enter" || e.key === " ") onStageClick(s.key); } : undefined}
              >
                <span
                  className={cn(
                    "flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold transition-colors",
                    isCompleted && "bg-emerald-100 text-emerald-700",
                    isCompleted && isClickable && "group-hover/step:bg-emerald-200",
                    isActive && "bg-teal-600 text-white",
                    isFuture && "bg-slate-100 text-slate-400"
                  )}
                >
                  {isCompleted ? (
                    <Check className="h-3 w-3" />
                  ) : (
                    idx + 1
                  )}
                </span>
                <span
                  className={cn(
                    "text-[10px] font-bold uppercase tracking-[0.15em] transition-colors",
                    isCompleted && "text-emerald-600",
                    isCompleted && isClickable && "group-hover/step:text-emerald-700 group-hover/step:underline",
                    isActive && "text-teal-700 font-black",
                    isFuture && "text-slate-300"
                  )}
                >
                  {s.label}
                </span>
              </div>

              {/* Arrow connector (not after last) */}
              {idx < STAGES.length - 1 && (
                <span className="text-slate-200 text-[10px]">→</span>
              )}
            </div>
          );
        })}
      </div>

      {/* Botón "Nuevo escaneo" con confirmación AlertDialog */}
      {currentIndex > 0 && onReset && (
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              disabled={resetting}
              className="text-[10px] text-slate-400 hover:text-slate-600 uppercase tracking-[0.15em] h-6 px-2 gap-1 rounded-none"
              title="Limpiar sesión y volver al inicio"
            >
              {resetting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <RotateCcw className="h-3 w-3" />
              )}
              {resetting ? "Reseteando..." : "Nuevo escaneo"}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>¿Reiniciar el proceso?</AlertDialogTitle>
              <AlertDialogDescription>
                Esto descartará todas las facturas procesadas y devolverá los archivos
                a sus carpetas originales. Esta acción no se puede deshacer.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel className="rounded-none text-xs uppercase tracking-[0.15em]">
                Cancelar
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={onReset}
                className="rounded-none text-xs uppercase tracking-[0.15em] bg-red-600 hover:bg-red-700"
              >
                Reiniciar
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
}
