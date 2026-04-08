"use client";

import React, { useState, useRef, useEffect } from "react";
import { ZoomIn, ZoomOut, RotateCcw, Loader2, ExternalLink, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ImageViewerProps {
  src: string;
  isLoading?: boolean;
  fileType?: "pdf" | "image";
}

export function ImageViewer({ src, isLoading = false, fileType = "image" }: ImageViewerProps) {
  const [scale, setScale] = useState(1);
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [imageError, setImageError] = useState(false);
  const [pdfError, setPdfError] = useState(false);
  const [pdfReady, setPdfReady] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const startRef = useRef({ x: 0, y: 0 });
  const pdfTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isPdf = fileType === "pdf";

  // Reset states when source changes
  useEffect(() => {
    setImageError(false);
    setPdfError(false);
    setPdfReady(false);

    // Para PDFs: timeout de detección de error (si no carga en 8s, mostrar fallback)
    if (isPdf && src) {
      if (pdfTimeoutRef.current) clearTimeout(pdfTimeoutRef.current);
      pdfTimeoutRef.current = setTimeout(() => {
        if (!pdfReady) setPdfError(true);
      }, 8000);
    }

    return () => {
      if (pdfTimeoutRef.current) clearTimeout(pdfTimeoutRef.current);
    };
  }, [src, isPdf]);

  const handleWheel = (e: React.WheelEvent) => {
    if (isPdf) return;
    e.preventDefault();
    const zoomSpeed = 0.1;
    const newScale = Math.max(0.5, Math.min(5, scale - e.deltaY * 0.001 * zoomSpeed * 10));
    setScale(newScale);
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    if (isPdf) return;
    setIsDragging(true);
    startRef.current = { x: e.clientX - position.x, y: e.clientY - position.y };
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (isPdf || !isDragging) return;
    const x = e.clientX - startRef.current.x;
    const y = e.clientY - startRef.current.y;
    setPosition({ x, y });
  };

  const handleMouseUp = () => {
    setIsDragging(false);
  };

  const handleZoomIn = () => setScale((s) => Math.min(5, s + 0.2));
  const handleZoomOut = () => setScale((s) => Math.max(0.5, s - 0.2));
  const handleReset = () => {
    setScale(1);
    setPosition({ x: 0, y: 0 });
  };

  if (isLoading) {
    return (
      <div className="relative w-full h-full bg-slate-50 flex flex-col">
        <div className="bg-white border-b px-6 py-3 flex items-center justify-between z-10">
          <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Documento Original</span>
        </div>
        <div className="flex-grow flex items-center justify-center bg-slate-100">
          <div className="text-center">
            <Loader2 className="h-8 w-8 animate-spin mx-auto mb-4 text-slate-400" />
            <p className="text-sm text-slate-500">Cargando documento...</p>
          </div>
        </div>
      </div>
    );
  }

  if (!src) {
    return (
      <div className="relative w-full h-full bg-slate-50 flex flex-col">
        <div className="bg-white border-b px-6 py-3 flex items-center justify-between z-10">
          <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Documento Original</span>
        </div>
        <div className="flex-grow flex items-center justify-center bg-slate-100">
          <div className="text-center">
            <FileText className="h-12 w-12 mx-auto mb-3 text-slate-300" />
            <p className="text-sm text-slate-400">No hay documento disponible</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative w-full h-full bg-slate-50 flex flex-col grayscale-0 hover:grayscale-0 transition-all">
      <div className="bg-white border-b px-6 py-3 flex items-center justify-between z-10">
        <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Documento Original</span>
        <div className="flex items-center gap-1">
          {!isPdf && (
            <>
              <Button variant="ghost" size="icon" onClick={handleZoomOut} aria-label="Alejar" className="h-8 w-8 hover:bg-slate-100">
                <ZoomOut className="h-4 w-4" />
              </Button>
              <span className="text-[10px] font-mono w-10 text-center font-bold text-slate-500">{Math.round(scale * 100)}%</span>
              <Button variant="ghost" size="icon" onClick={handleZoomIn} aria-label="Acercar" className="h-8 w-8 hover:bg-slate-100">
                <ZoomIn className="h-4 w-4" />
              </Button>
              <div className="w-[1px] h-3 bg-slate-200 mx-1" />
              <Button variant="ghost" size="icon" onClick={handleReset} aria-label="Restablecer zoom" className="h-8 w-8 hover:bg-slate-100 text-slate-400">
                <RotateCcw className="h-3 w-3" />
              </Button>
            </>
          )}
          {/* Botón abrir en nueva pestaña siempre disponible */}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 hover:bg-slate-100 text-slate-400"
            onClick={() => window.open(src, "_blank")}
            aria-label="Abrir en nueva pestaña"
            title="Abrir en nueva pestaña"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {isPdf ? (
        <div className="flex-grow overflow-hidden relative bg-white">
          {pdfError ? (
            <div className="flex-grow flex items-center justify-center bg-slate-50 h-full">
              <div className="text-center">
                <FileText className="h-12 w-12 mx-auto mb-3 text-slate-300" />
                <p className="text-sm text-slate-500 mb-1">No se pudo cargar el PDF inline</p>
                <p className="text-xs text-slate-400 mb-4">Puedes abrirlo en una nueva pestaña</p>
                <div className="flex gap-2 justify-center">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => { setPdfError(false); setPdfReady(false); }}
                  >
                    Reintentar
                  </Button>
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => window.open(src, "_blank")}
                  >
                    <ExternalLink className="h-3 w-3 mr-1" />
                    Abrir en nueva pestaña
                  </Button>
                </div>
              </div>
            </div>
          ) : (
            <>
              {/* object + embed: renderiza PDFs cross-origin sin sandbox restrictions */}
              <object
                data={src}
                type="application/pdf"
                className="w-full h-full"
                onLoad={() => {
                  setPdfReady(true);
                  if (pdfTimeoutRef.current) clearTimeout(pdfTimeoutRef.current);
                }}
                onError={() => setPdfError(true)}
              >
                <embed src={src} type="application/pdf" className="w-full h-full" />
              </object>
              {!pdfReady && !pdfError && (
                <div className="absolute inset-0 flex items-center justify-center bg-slate-50">
                  <div className="text-center">
                    <Loader2 className="h-6 w-6 animate-spin mx-auto mb-2 text-slate-400" />
                    <p className="text-xs text-slate-400">Cargando PDF...</p>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      ) : (
        <div
          ref={containerRef}
          className={cn(
            "flex-grow overflow-hidden cursor-grab active:cursor-grabbing relative bg-slate-100",
            isDragging && "cursor-grabbing"
          )}
          onWheel={handleWheel}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
        >
          <div
            className="absolute inset-0 flex items-start justify-center transition-transform duration-75 ease-out select-none"
            style={{
              transform: `translate(${position.x}px, ${position.y}px) scale(${scale})`,
              transformOrigin: "center center"
            }}
          >
            {imageError ? (
              <div className="flex items-center justify-center w-full h-full">
                <div className="text-center">
                  <FileText className="h-12 w-12 mx-auto mb-3 text-slate-300" />
                  <p className="text-sm text-slate-500 mb-2">No se pudo cargar el documento</p>
                  <Button variant="outline" size="sm" onClick={() => { setImageError(false); }}>
                    Reintentar
                  </Button>
                </div>
              </div>
            ) : (
              <img
                key={src}
                src={src}
                alt="Invoice preview"
                onError={() => setImageError(true)}
                className="max-w-[70%] h-auto shadow-sm bg-white m-12 pointer-events-none"
                crossOrigin="anonymous"
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
