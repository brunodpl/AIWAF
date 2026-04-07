"use client";

import { cn } from "@/lib/utils";
import { Check, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

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
  // FIX #7: Callbacks para navegación y reset
  onStageClick?: (stage: Stage) => void;
  onReset?: () => void;
}

export function StageIndicator({ stage, className, onStageClick, onReset }: StageIndicatorProps) {
  const currentIndex = STAGES.findIndex((s) => s.key === stage);

  return (
    <div className={cn("flex items-center justify-between gap-2 text-xs", className)}>
      <div className="flex items-center gap-2">
        {STAGES.map((s, idx) => {
          const isCompleted = idx < currentIndex;
          const isActive = idx === currentIndex;
          const isFuture = idx > currentIndex;
          // FIX #7: los pasos completados son navegables (cursor-pointer + onClick)
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

      {/* FIX #6: Botón "Nuevo escaneo" — reset global visible solo cuando no estamos en el inicio */}
      {currentIndex > 0 && onReset && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onReset}
          className="text-[10px] text-slate-400 hover:text-slate-600 uppercase tracking-[0.15em] h-6 px-2 gap-1 rounded-none"
          title="Limpiar sesión y volver al inicio"
        >
          <RotateCcw className="h-3 w-3" />
          Nuevo escaneo
        </Button>
      )}
    </div>
  );
}
