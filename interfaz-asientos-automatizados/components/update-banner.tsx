"use client";

import { useEffect, useState } from "react";
import { Download, X, Loader2, CheckCircle2, AlertTriangle } from "lucide-react";
import {
  fetchLatestVersion,
  fetchSystemVersion,
  requestSystemUpdate,
  type LatestVersion,
} from "@/lib/api";

const DISMISS_KEY = "aiwaf_update_dismissed_for_version";
const POLL_INTERVAL_MS = 5_000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

type State =
  | { phase: "idle" }
  | { phase: "requesting" }
  | { phase: "in_progress"; startedAt: number }
  | { phase: "success" }
  | { phase: "error"; message: string };

export function UpdateBanner() {
  const [info, setInfo] = useState<LatestVersion | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [state, setState] = useState<State>({ phase: "idle" });

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const latest = await fetchLatestVersion();
        if (cancelled) return;
        setInfo(latest);
        const dismissedFor =
          typeof window !== "undefined"
            ? window.localStorage.getItem(DISMISS_KEY)
            : null;
        if (dismissedFor && latest.version && dismissedFor === latest.version) {
          setDismissed(true);
        }
      } catch {
        /* offline — silencioso */
      }
    }
    check();
    const intervalId = setInterval(check, 10 * 60 * 1000);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    if (state.phase !== "in_progress" || !info?.version) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const startedAt = state.startedAt;
    const targetVersion = info.version;

    const poll = async () => {
      if (cancelled) return;
      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        setState({
          phase: "error",
          message:
            "La actualización está tardando más de lo esperado. Comprueba el estado en la pestaña Diagnóstico.",
        });
        return;
      }
      try {
        const v = await fetchSystemVersion();
        if (cancelled) return;
        if (v.version === targetVersion) {
          setState({ phase: "success" });
          setTimeout(() => window.location.reload(), 3_000);
          return;
        }
      } catch {
        /* contenedor reiniciándose — esperado, seguimos */
      }
      if (!cancelled) timer = setTimeout(poll, POLL_INTERVAL_MS);
    };
    timer = setTimeout(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [state, info?.version]);

  if (!info || !info.update_available || dismissed) return null;

  const handleDismiss = () => {
    if (info.version) {
      window.localStorage.setItem(DISMISS_KEY, info.version);
    }
    setDismissed(true);
  };

  const handleUpdate = async () => {
    setState({ phase: "requesting" });
    try {
      await requestSystemUpdate();
      setState({ phase: "in_progress", startedAt: Date.now() });
    } catch (err) {
      setState({
        phase: "error",
        message: err instanceof Error ? err.message : "Error desconocido",
      });
    }
  };

  return (
    <div className="bg-emerald-50 border-b border-emerald-200 px-4 py-2 flex items-center justify-between text-xs">
      <div className="flex items-center gap-2 text-emerald-900">
        <Download className="h-4 w-4" />
        <span className="font-medium">
          Actualización disponible: {info.version}
        </span>
        <span className="text-emerald-700">(instalada: {info.current})</span>
        {info.changelog && (
          <span className="text-emerald-700 hidden md:inline">
            — {info.changelog.slice(0, 120)}
          </span>
        )}
      </div>
      <div className="flex items-center gap-3">
        {state.phase === "idle" && (
          <>
            <button
              onClick={handleUpdate}
              className="font-semibold uppercase tracking-wider text-emerald-900 border border-emerald-700 rounded px-3 py-1 hover:bg-emerald-100"
            >
              Actualizar ahora
            </button>
            <button
              onClick={handleDismiss}
              aria-label="Descartar aviso de actualización"
              className="text-emerald-700 hover:text-emerald-900"
            >
              <X className="h-4 w-4" />
            </button>
          </>
        )}
        {state.phase === "requesting" && (
          <span className="flex items-center gap-1 text-emerald-800">
            <Loader2 className="h-4 w-4 animate-spin" /> Solicitando…
          </span>
        )}
        {state.phase === "in_progress" && (
          <span className="flex items-center gap-1 text-emerald-800">
            <Loader2 className="h-4 w-4 animate-spin" />
            Actualizando… (1-3 min, no cierres esta ventana)
          </span>
        )}
        {state.phase === "success" && (
          <span className="flex items-center gap-1 text-emerald-900 font-semibold">
            <CheckCircle2 className="h-4 w-4" /> Actualizado a {info.version}.
            Recargando…
          </span>
        )}
        {state.phase === "error" && (
          <span className="flex items-center gap-1 text-red-700">
            <AlertTriangle className="h-4 w-4" />
            {state.message}
          </span>
        )}
      </div>
    </div>
  );
}
