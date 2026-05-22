"use client";

import { cn } from "@/lib/utils";
import {
  CheckCircle2,
  Circle,
  Loader2,
  AlertTriangle,
  XCircle,
  Ban,
} from "lucide-react";
import { prettifyBatchFile } from "@/lib/splitter-naming";
import { libroLabel } from "@/lib/libros";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { BatchFileEntry, BatchFileStatus, PipelineBatch } from "@/lib/types";

interface BatchOverviewProps {
  batch: PipelineBatch | null;
}

const STATUS_META: Record<
  BatchFileStatus,
  { Icon: typeof CheckCircle2; color: string; label: string; dim: boolean }
> = {
  pending:    { Icon: Circle,         color: "text-slate-300",  label: "En cola",   dim: true  },
  processing: { Icon: Loader2,        color: "text-teal-600",   label: "Procesando", dim: false },
  done:       { Icon: CheckCircle2,   color: "text-emerald-500",label: "Hecha",     dim: false },
  review:     { Icon: AlertTriangle,  color: "text-amber-500",  label: "Revisión",  dim: false },
  confirmed:  { Icon: CheckCircle2,   color: "text-emerald-600",label: "Confirmada",dim: false },
  blocked:    { Icon: Ban,            color: "text-red-500",    label: "Bloqueada", dim: false },
  error:      { Icon: XCircle,        color: "text-red-500",    label: "Error",     dim: false },
};

function BatchRow({ entry }: { entry: BatchFileEntry }) {
  const meta = STATUS_META[entry.status] ?? STATUS_META.pending;
  const { Icon } = meta;
  const spin = entry.status === "processing";
  return (
    <li
      className={cn(
        "flex items-center gap-3 px-3 py-1.5 rounded text-xs",
        meta.dim && "opacity-60",
      )}
    >
      <Icon className={cn("h-3.5 w-3.5 flex-shrink-0", meta.color, spin && "animate-spin")} />
      <span className="flex-1 truncate text-slate-700 font-mono" title={entry.filename}>
        {prettifyBatchFile(entry)}
      </span>
      <span className="text-[10px] uppercase tracking-[0.1em] text-slate-400 flex-shrink-0">
        {meta.label}
      </span>
    </li>
  );
}

export function BatchOverview({ batch }: BatchOverviewProps) {
  if (!batch || !batch.in_flight || batch.books.length === 0) {
    return null;
  }
  return (
    <div className="mt-6 w-full max-w-md mx-auto text-left">
      {/* Scroll interno: con lotes grandes (60+ facturas) la lista no debe
          desbordar el viewport — cabe en "una página" y hace scroll dentro. */}
      <ScrollArea className="max-h-[55vh]">
        <div className="space-y-4 pr-3">
          {batch.books.map((book) => (
            <section key={book.book_id}>
              <header className="flex items-baseline justify-between mb-1.5">
                <h3 className="text-[10px] font-black uppercase tracking-[0.15em] text-slate-600">
                  {libroLabel(book.book_id)}
                </h3>
                <span className="text-[10px] font-mono text-slate-400">
                  {book.files.length} factura{book.files.length === 1 ? "" : "s"}
                </span>
              </header>
              <ul className="border border-slate-200 rounded divide-y divide-slate-100 bg-white">
                {book.files.map((entry) => (
                  <BatchRow key={entry.doc_id} entry={entry} />
                ))}
              </ul>
            </section>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
