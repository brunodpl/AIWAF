"use client";

import { cn } from "@/lib/utils";
import { Check } from "lucide-react";

type Stage = "books" | "processing" | "review" | "export";

const STAGES: { key: Stage; label: string }[] = [
  { key: "books", label: "Gesti\u00f3n" },
  { key: "processing", label: "Escaneando" },
  { key: "review", label: "Revisi\u00f3n" },
  { key: "export", label: "Exportar" },
];

interface StageIndicatorProps {
  stage: Stage;
  className?: string;
}

export function StageIndicator({ stage, className }: StageIndicatorProps) {
  const currentIndex = STAGES.findIndex((s) => s.key === stage);

  return (
    <div className={cn("flex items-center gap-2 text-xs", className)}>
      {STAGES.map((s, idx) => {
        const isCompleted = idx < currentIndex;
        const isActive = idx === currentIndex;
        const isFuture = idx > currentIndex;

        return (
          <div key={s.key} className="flex items-center gap-2">
            {/* Stage number/indicator */}
            <div className="flex items-center gap-1.5">
              <span
                className={cn(
                  "flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold",
                  isCompleted && "bg-emerald-100 text-emerald-700",
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
                  "text-[10px] font-bold uppercase tracking-[0.15em]",
                  isCompleted && "text-emerald-600",
                  isActive && "text-teal-700 font-black",
                  isFuture && "text-slate-300"
                )}
              >
                {s.label}
              </span>
            </div>

            {/* Arrow connector (not after last) */}
            {idx < STAGES.length - 1 && (
              <span className="text-slate-200 text-[10px]">\u2192</span>
            )}
          </div>
        );
      })}
    </div>
  );
}
