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
  onStageClick?: (stage: Stage) => void;
  onReset?: () => void;
}

export function StageIndicator({ stage, className, onStageClick, onReset }: StageIndicatorProps) {
  const currentIndex = STAGES.findIndex((s) => s.key === stage);

  return (
    <div className={cn("flex items-center justify-between", className)}>
      <div className="flex items-center gap-2 text-xs">
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
                onClick={() => isClickable && onStageClick?.(s.key)}
                role={isClickable ? "button" : undefined}
                aria-label={isClickable ? `Volver a ${s.label}` : undefined}
                tabIndex={isClickable ? 0 : undefined}
                onKeyDown={(e) => e.key === "Enter" && isClickable && onStageClick?.(s.key)}
              >
                <span
                  className={cn(
                    "flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold transition-all",
                    isCompleted && "bg-emerald-100 text-emerald-700 group-hover/step:bg-emerald-200",
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
                    isCompleted && "text-emerald-600 group-hover/step:text-emerald-700",
                    isActive && "text-teal-700 font-black",
                    isFuture && "text-slate-300"
                  )}
                >
                  {s.label}
                </span>
              </div>

              {idx < STAGES.length - 1 && (
                <span className="text-slate-200 text-[10px]">→</span>
              )}
            </div>
          );
        })}
      </div>

      {/* Reset button — only shown when not at initial books stage */}
      {stage !== "books" && onReset && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onReset}
          className="text-[10px] uppercase tracking-[0.15em] text-slate-300 hover:text-slate-600 gap-1"
          aria-label="Nuevo escaneo: volver al inicio y limpiar estado"
        >
          <RotateCcw className="h-3 w-3" />
          Nuevo escaneo
        </Button>
      )}
    </div>
  );
}
