"""Orquestador de la Fase 1 (splitter pre-OCR).

Recorre los PDFs de la carpeta de entrada y, cuando un PDF tiene varias
páginas, pregunta a Gemini Vision cómo dividirlo en facturas individuales.
Los PDFs resultantes se escriben en la misma carpeta con sufijo
``__{i}of{N}`` y el original se archiva en ``_originales/`` junto con un
manifiesto ``.split.json`` para trazabilidad.

PDFs de una sola página se ignoran (no se llama a Gemini).
Si Gemini falla, el PDF se deja intacto: el pipeline lo procesará como
hoy y la incidencia caerá en revisión humana.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.config import settings as get_settings
from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model

from .gemini_splitter import SplitDecision, detect_splits
from .pdf_utils import extract_pages, page_count, read_bytes, sha256_file

logger = logging.getLogger("pipeline.splitter")


ORIGINALES_DIRNAME = "_originales"


@dataclass
class SplitOutcome:
    """Resumen del split aplicado a un PDF."""

    source: str
    status: str  # "split" | "single_page" | "single_factura" | "error"
    n_facturas: int = 0
    n_pages: int = 0
    outputs: List[str] = field(default_factory=list)
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    confidence_min: Optional[float] = None
    confidence_avg: Optional[float] = None


def run_split(folder_path: str, client=None, model: Optional[str] = None) -> List[SplitOutcome]:
    """Ejecuta el splitter sobre todos los PDFs de ``folder_path``.

    Idempotente: si un PDF ya está en ``_originales/`` no se vuelve a tocar
    (la subcarpeta se ignora). Los PDFs producidos por una corrida previa
    (sufijo ``__\\dof\\d``) se reconocen, pero si vuelven a entrar al splitter
    al ser de 1 página el ``page_count`` los descarta sin llamar a Gemini.
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder_path}")

    cfg = get_settings()
    if model is None:
        model = cfg.gemini_ocr_model

    pdfs = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )

    outcomes: List[SplitOutcome] = []
    lazy_client = client
    for pdf in pdfs:
        try:
            n_pages = page_count(pdf)
        except Exception as e:
            logger.warning("[splitter] no se pudo leer %s: %s", pdf.name, e)
            outcomes.append(SplitOutcome(
                source=str(pdf), status="error", error=f"page_count: {e}",
            ))
            continue

        if n_pages <= 1:
            outcomes.append(SplitOutcome(
                source=str(pdf), status="single_page", n_pages=n_pages,
                n_facturas=1, outputs=[str(pdf)],
            ))
            continue

        if lazy_client is None:
            lazy_client = _init_gemini_model()

        try:
            decision = detect_splits(
                lazy_client, read_bytes(pdf), model=model, n_pages_total=n_pages,
            )
        except Exception as e:
            logger.error(
                "[splitter] Gemini falló para %s: %s — se deja sin dividir",
                pdf.name, e, exc_info=True,
            )
            outcomes.append(SplitOutcome(
                source=str(pdf), status="error", n_pages=n_pages,
                error=f"gemini: {e}",
            ))
            continue

        if len(decision.groups) <= 1:
            outcomes.append(SplitOutcome(
                source=str(pdf), status="single_factura",
                n_pages=n_pages, n_facturas=1, outputs=[str(pdf)],
                latency_ms=decision.latency_ms,
            ))
            continue

        outcome = _apply_split(pdf, n_pages, decision)
        outcomes.append(outcome)

    _write_split_log(cfg, outcomes)
    return outcomes


def _apply_split(pdf: Path, n_pages: int, decision: SplitDecision) -> SplitOutcome:
    originales = pdf.parent / ORIGINALES_DIRNAME
    originales.mkdir(exist_ok=True)

    source_sha = sha256_file(pdf)
    stem = pdf.stem
    n = len(decision.groups)

    outputs: List[Path] = []
    manifest_facturas: list[dict] = []
    confidences: list[float] = []

    for i, group in enumerate(decision.groups, start=1):
        out_path = pdf.parent / f"{stem}__{i}of{n}.pdf"
        # Si por alguna colisión existe ya, lo sobrescribimos: el contenido
        # se deriva determinísticamente del origen + páginas.
        extract_pages(pdf, list(group.pages), out_path)
        outputs.append(out_path)
        if group.confidence is not None:
            confidences.append(group.confidence)
        manifest_facturas.append({
            "output_pdf": out_path.name,
            "paginas_originales": list(group.pages),
            "emisor_cif_aparente": group.emisor_cif_aparente,
            "confidence": group.confidence,
        })

    manifest = {
        "origen_pdf": pdf.name,
        "origen_sha256": source_sha,
        "origen_paginas": n_pages,
        "n_facturas": n,
        "gemini_model": decision.model_used,
        "latency_ms": decision.latency_ms,
        "split_timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "facturas": manifest_facturas,
    }

    archived = originales / pdf.name
    if archived.exists():
        archived.unlink()
    os.replace(pdf, archived)

    manifest_path = originales / f"{stem}.split.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    confidence_min = min(confidences) if confidences else None
    confidence_avg = sum(confidences) / len(confidences) if confidences else None

    logger.info(
        "[splitter] %s → %d facturas (páginas=%s, latency=%dms, conf_min=%s)",
        pdf.name, n, [list(g.pages) for g in decision.groups],
        decision.latency_ms, confidence_min,
    )

    return SplitOutcome(
        source=str(archived),
        status="split",
        n_facturas=n,
        n_pages=n_pages,
        outputs=[str(p) for p in outputs],
        latency_ms=decision.latency_ms,
        confidence_min=confidence_min,
        confidence_avg=confidence_avg,
    )


def _write_split_log(cfg, outcomes: List[SplitOutcome]) -> None:
    """Append-only JSONL con un registro por PDF procesado por el splitter."""
    if not outcomes:
        return
    try:
        audit_dir = Path(cfg.audit_path())
        audit_dir.mkdir(parents=True, exist_ok=True)
        fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = audit_dir / f"splits_{fecha}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for o in outcomes:
                rec = {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    **asdict(o),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[splitter] no se pudo escribir splits_*.jsonl", exc_info=True)
