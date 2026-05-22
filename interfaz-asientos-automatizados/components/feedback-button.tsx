"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { MessageSquareWarning, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { submitFeedback, type FeedbackTipo } from "@/lib/api";
import { cn } from "@/lib/utils";

const TIPOS: { value: FeedbackTipo; label: string }[] = [
  { value: "problema", label: "Problema" },
  { value: "recomendacion", label: "Recomendación" },
  { value: "pregunta", label: "Pregunta" },
];

export function FeedbackButton({ className }: { className?: string }) {
  const [open, setOpen] = useState(false);
  const [tipo, setTipo] = useState<FeedbackTipo>("problema");
  const [descripcion, setDescripcion] = useState("");
  const [incluirLogs, setIncluirLogs] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const reset = () => {
    setTipo("problema");
    setDescripcion("");
    setIncluirLogs(true);
  };

  const handleSubmit = async () => {
    const trimmed = descripcion.trim();
    if (trimmed.length < 5) {
      toast.error("Describe el problema con un poco más de detalle (mínimo 5 caracteres)");
      return;
    }
    setSubmitting(true);
    try {
      const navegador = typeof window !== "undefined" ? window.navigator.userAgent : undefined;
      const result = await submitFeedback({
        tipo,
        descripcion: trimmed,
        incluir_logs: incluirLogs,
        navegador,
      });
      if (result.delivered) {
        toast.success("Mensaje enviado. Gracias por reportarlo.");
      } else {
        toast.warning(
          "Mensaje guardado localmente. No se pudo entregar al equipo de AIWAF; se reintentará automáticamente.",
        );
      }
      reset();
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Error enviando el mensaje");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Enviar Problema o Recomendación"
        className={cn(
          "fixed bottom-4 right-4 z-50 rounded-full shadow-lg bg-slate-900 hover:bg-slate-700 text-white px-4 py-2 text-xs uppercase tracking-[0.1em]",
          className,
        )}
      >
        <MessageSquareWarning className="h-4 w-4 sm:mr-2" />
        <span className="hidden sm:inline">Enviar Problema o Recomendación</span>
      </Button>

      <Dialog
        open={open}
        onOpenChange={(v) => {
          if (!submitting) setOpen(v);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Enviar Problema o Recomendación</DialogTitle>
            <DialogDescription>
              Tu mensaje llegará directamente al equipo de AIWAF para que podamos ayudarte cuanto antes.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label className="text-xs uppercase tracking-wider text-slate-600">Tipo</Label>
              <div className="flex gap-2">
                {TIPOS.map((t) => (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => setTipo(t.value)}
                    className={
                      "flex-1 px-3 py-2 text-xs uppercase tracking-wider border transition-colors " +
                      (tipo === t.value
                        ? "bg-slate-900 text-white border-slate-900"
                        : "bg-white text-slate-700 border-slate-200 hover:bg-slate-50")
                    }
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="feedback-desc" className="text-xs uppercase tracking-wider text-slate-600">
                Descripción
              </Label>
              <Textarea
                id="feedback-desc"
                value={descripcion}
                onChange={(e) => setDescripcion(e.target.value)}
                placeholder="Cuéntanos qué ha pasado o qué te gustaría que mejorásemos..."
                rows={5}
                maxLength={4000}
              />
              <p className="text-[10px] text-slate-400">{descripcion.length}/4000</p>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox
                id="feedback-logs"
                checked={incluirLogs}
                onCheckedChange={(v) => setIncluirLogs(v === true)}
              />
              <Label htmlFor="feedback-logs" className="text-xs text-slate-700 cursor-pointer">
                Adjuntar últimas 50 líneas de log automáticamente (recomendado para problemas técnicos)
              </Label>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)} disabled={submitting}>
              Cancelar
            </Button>
            <Button onClick={handleSubmit} disabled={submitting}>
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Enviando...
                </>
              ) : (
                "Enviar"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
