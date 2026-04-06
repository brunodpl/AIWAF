"use client";

import React, { useState, useRef } from "react";
import { ZoomIn, ZoomOut, RotateCcw, Loader2 } from "lucide-react";
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
  const containerRef = useRef<HTMLDivElement>(null);
  const startRef = useRef({ x: 0, y: 0 });

  const isPdf = fileType === "pdf";

  const handleWheel = (e: React.WheelEvent) => {
    if (isPdf) return; // Don't handle wheel for PDFs (browser handles it)
    e.preventDefault();
    const zoomSpeed = 0.1;
    const newScale = Math.max(0.5, Math.min(5, scale - e.deltaY * 0.001 * zoomSpeed * 10));
    setScale(newScale);
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    if (isPdf) return; // Don't handle drag for PDFs
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

  // Show loading state while document is being fetched
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

  // Show placeholder if no valid source
  if (!src || src === "/placeholder.jpg") {
    return (
      <div className="relative w-full h-full bg-slate-50 flex flex-col">
        <div className="bg-white border-b px-6 py-3 flex items-center justify-between z-10">
          <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Documento Original</span>
        </div>
        <div className="flex-grow flex items-center justify-center bg-slate-100">
          <div className="text-center">
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
        {!isPdf && (
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="icon" onClick={handleZoomOut} className="h-8 w-8 hover:bg-slate-100">
              <ZoomOut className="h-4 w-4" />
            </Button>
            <span className="text-[10px] font-mono w-10 text-center font-bold text-slate-500">{Math.round(scale * 100)}%</span>
            <Button variant="ghost" size="icon" onClick={handleZoomIn} className="h-8 w-8 hover:bg-slate-100">
              <ZoomIn className="h-4 w-4" />
            </Button>
            <div className="w-[1px] h-3 bg-slate-200 mx-1" />
            <Button variant="ghost" size="icon" onClick={handleReset} className="h-8 w-8 hover:bg-slate-100 text-slate-400">
              <RotateCcw className="h-3 w-3" />
            </Button>
          </div>
        )}
      </div>

      {isPdf ? (
        // PDF Viewer - using iframe for native browser PDF support
        <div className="flex-grow overflow-hidden relative bg-white">
          <iframe
            src={src}
            className="w-full h-full border-0"
            title="Invoice PDF"
          />
        </div>
      ) : (
        // Image Viewer with zoom and pan
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
            <img
              src={src}
              alt="Invoice preview"
              className="max-w-[70%] h-auto shadow-sm bg-white m-12 pointer-events-none"
            />
          </div>
        </div>
      )}
    </div>
  );
}
