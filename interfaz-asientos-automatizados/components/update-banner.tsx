"use client";

import { useEffect, useState } from "react";
import { Download, X } from "lucide-react";
import { fetchLatestVersion, type LatestVersion } from "@/lib/api";

const DISMISS_KEY = "aiwaf_update_dismissed_for_version";

export function UpdateBanner() {
  const [info, setInfo] = useState<LatestVersion | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const latest = await fetchLatestVersion();
        if (cancelled) return;
        setInfo(latest);
        // Si el usuario ya descartó este número de versión, no volvemos a mostrar
        const dismissedFor = typeof window !== "undefined"
          ? window.localStorage.getItem(DISMISS_KEY)
          : null;
        if (dismissedFor && latest.version && dismissedFor === latest.version) {
          setDismissed(true);
        }
      } catch {
        /* offline o servidor de releases caído — silenciamos */
      }
    }
    check();
    // Recheck cada hora
    const intervalId = setInterval(check, 60 * 60 * 1000);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, []);

  if (!info || !info.update_available || dismissed) return null;

  const handleDismiss = () => {
    if (info.version) {
      window.localStorage.setItem(DISMISS_KEY, info.version);
    }
    setDismissed(true);
  };

  return (
    <div className="bg-emerald-50 border-b border-emerald-200 px-4 py-2 flex items-center justify-between text-xs">
      <div className="flex items-center gap-2 text-emerald-900">
        <Download className="h-4 w-4" />
        <span className="font-medium">
          Actualización disponible: {info.version}
        </span>
        <span className="text-emerald-700">
          (instalada: {info.current})
        </span>
        {info.changelog && (
          <span className="text-emerald-700 hidden md:inline">— {info.changelog.slice(0, 120)}</span>
        )}
      </div>
      <div className="flex items-center gap-3">
        <a
          href="https://aiwaf-releases.pages.dev"
          target="_blank"
          rel="noopener noreferrer"
          className="font-semibold uppercase tracking-wider text-emerald-900 hover:underline"
        >
          Cómo actualizar
        </a>
        <button
          onClick={handleDismiss}
          aria-label="Descartar aviso de actualización"
          className="text-emerald-700 hover:text-emerald-900"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
