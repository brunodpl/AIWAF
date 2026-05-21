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
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src import state_writer
from src.config import settings as get_settings
from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model

from .gemini_splitter import SplitDecision, detect_splits
from .pdf_utils import extract_pages, page_count, read_bytes, sha256_file

logger = logging.getLogger("pipeline.splitter")


ORIGINALES_DIRNAME = "_originales"

# Backoff entre reintentos cuando Gemini Vision devuelve error transitorio.
# Pequeño y fijo: el splitter corre en /upload síncrono y no queremos
# inflar la latencia percibida.
_RETRY_BACKOFF_SECONDS = 1.0


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


def split_single_file(
    pdf_path: Path,
    *,
    client=None,
    model: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> SplitOutcome:
    """Ejecuta el splitter Fase 1 sobre **un único** PDF.

    Punto de entrada usado por:

    - ``/api/books/{book_id}/upload`` (pre-scan síncrono por archivo).
    - ``run_split`` (safeguard durante ``pipeline.run`` — itera carpeta).

    Política de reintentos: si Gemini Vision lanza, reintenta hasta
    ``max_attempts`` veces (default = ``settings().prescan_max_attempts``)
    con backoff fijo de 1s. Tras agotar intentos devuelve ``status="error"``
    con el último mensaje — el caller decide qué hacer con el sidecar.

    NO escribe el JSONL de auditoría — eso es responsabilidad del orquestador
    (``run_split`` lo hace tras procesar la carpeta entera). El caller del
    upload puede llamar a ``_write_split_log`` con la lista de outcomes
    agregados al final del request.
    """
    pdf = Path(pdf_path)
    cfg = get_settings()
    if model is None:
        model = cfg.gemini_ocr_model
    if max_attempts is None:
        max_attempts = max(1, int(cfg.prescan_max_attempts))

    try:
        n_pages = page_count(pdf)
    except Exception as e:
        logger.warning("[splitter] no se pudo leer %s: %s", pdf.name, e)
        return SplitOutcome(
            source=str(pdf), status="error", error=f"page_count: {e}",
        )

    if n_pages <= 1:
        return SplitOutcome(
            source=str(pdf), status="single_page", n_pages=n_pages,
            n_facturas=1, outputs=[str(pdf)],
        )

    # PDF multi-página: hace falta Gemini. Lazy init del cliente.
    active_client = client if client is not None else _init_gemini_model()

    last_error: Optional[Exception] = None
    decision: Optional[SplitDecision] = None
    for attempt in range(1, max_attempts + 1):
        try:
            decision = detect_splits(
                active_client, read_bytes(pdf), model=model, n_pages_total=n_pages,
            )
            break
        except Exception as e:
            last_error = e
            logger.warning(
                "[splitter] Gemini falló intento %d/%d para %s: %s",
                attempt, max_attempts, pdf.name, e,
            )
            if attempt < max_attempts:
                time.sleep(_RETRY_BACKOFF_SECONDS)

    if decision is None:
        logger.error(
            "[splitter] Gemini agotó %d intentos para %s — última excepción: %s",
            max_attempts, pdf.name, last_error, exc_info=last_error,
        )
        return SplitOutcome(
            source=str(pdf), status="error", n_pages=n_pages,
            error=f"gemini: {last_error}",
        )

    if len(decision.groups) <= 1:
        return SplitOutcome(
            source=str(pdf), status="single_factura",
            n_pages=n_pages, n_facturas=1, outputs=[str(pdf)],
            latency_ms=decision.latency_ms,
        )

    return _apply_split(pdf, n_pages, decision)


def run_split(folder_path: str, client=None, model: Optional[str] = None) -> List[SplitOutcome]:
    """Ejecuta el splitter sobre todos los PDFs de ``folder_path``.

    Idempotente: si un PDF ya está en ``_originales/`` no se vuelve a tocar
    (la subcarpeta se ignora). Los PDFs producidos por una corrida previa
    (sufijo ``__\\dof\\d``) se reconocen, pero si vuelven a entrar al splitter
    al ser de 1 página el ``page_count`` los descarta sin llamar a Gemini.

    Delega cada PDF a ``split_single_file`` (que aplica reintentos internos).
    Se mantiene como punto de entrada del orquestador y como safeguard
    durante ``pipeline.run`` para PDFs legacy que no pasaron por pre-scan.
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
        # Reusar lazy client entre PDFs de la misma carpeta evita pagar
        # el init de Gemini múltiples veces en runs legacy.
        if lazy_client is None:
            try:
                # Si el primer PDF es single-page, split_single_file no usa
                # cliente — diferimos el init hasta que aparezca un multi-página.
                pages_first = page_count(pdf)
            except Exception:
                pages_first = 1
            if pages_first > 1:
                lazy_client = _init_gemini_model()
        outcomes.append(split_single_file(pdf, client=lazy_client, model=model))

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

    # Cadena de custodia en .state.json (best-effort, no rompe el split):
    # 1) marcar el sidecar del original como `split` con la lista de doc_ids
    #    derivados (evita ghosts en find_folder_by_doc_id y permite skipear
    #    en runs posteriores vía is_terminal()).
    # 2) inicializar sidecars `uploaded` para cada split, para que el endpoint
    #    /api/pipeline/batch los liste como pendientes desde el primer poll
    #    aunque OCR aún no haya llegado a ellos.
    output_doc_ids = [p.stem for p in outputs]
    _update_state_for_split(
        pdf, archived, outputs, output_doc_ids, source_sha,
    )

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


def _update_state_for_split(
    original_pdf: Path,
    archived_pdf: Path,
    splits: List[Path],
    output_doc_ids: List[str],
    source_sha: str,
) -> None:
    """Marca el sidecar original como `split` y crea sidecars `uploaded` por cada split.

    Best-effort: cualquier fallo se loggea pero no interrumpe el splitter,
    los PDFs ya están en disco y el pipeline puede continuar.
    """
    try:
        cfg = get_settings()
        asientos_root = Path(cfg.asientos_path())
        libros_base = Path(cfg.libros_base).resolve()
        libro_short = original_pdf.parent.name
    except Exception:
        logger.warning(
            "[splitter] no se pudo resolver settings para actualizar .state.json",
            exc_info=True,
        )
        return

    # 1) Sidecar original → status=split (solo si existía).
    original_doc_id = original_pdf.stem
    try:
        original_folder = state_writer.find_folder_by_doc_id(
            asientos_root, original_doc_id
        )
        if original_folder is not None:
            try:
                rel_archived = archived_pdf.resolve().relative_to(libros_base)
                archived_rel_str = str(rel_archived).replace(os.sep, "/")
            except (ValueError, OSError):
                archived_rel_str = archived_pdf.name
            state_writer.append(original_folder, {
                "status": "split",
                "split_into": list(output_doc_ids),
                "archived_pdf": archived_rel_str,
            })
    except Exception:
        logger.warning(
            "[splitter] no se pudo marcar sidecar original como split: %s",
            original_doc_id, exc_info=True,
        )

    # 2) Sidecar uploaded por cada split.
    for split_pdf, doc_id in zip(splits, output_doc_ids):
        folder_name = f"{libro_short}_{doc_id}"
        asiento_folder = asientos_root / folder_name
        try:
            if state_writer.find_folder_by_doc_id(asientos_root, doc_id) is not None:
                # Idempotencia: si por alguna razón ya existe sidecar para
                # este doc_id, no lo tocamos (caso rerun manual).
                continue
            try:
                rel_pdf = split_pdf.resolve().relative_to(libros_base)
                pdf_rel_str = str(rel_pdf).replace(os.sep, "/")
            except (ValueError, OSError):
                pdf_rel_str = split_pdf.name
            try:
                size_bytes = split_pdf.stat().st_size
            except OSError:
                size_bytes = 0
            state_writer.init_uploaded(
                asiento_folder,
                doc_id=doc_id,
                file_origin=pdf_rel_str,
                # Reusa el sha256 del origen + sufijo del índice para tener un
                # hash determinista por split sin re-leer el fichero entero.
                sha256_file=f"{source_sha}#{doc_id}",
                size_bytes=size_bytes,
            )
        except Exception:
            logger.warning(
                "[splitter] no se pudo inicializar sidecar para %s",
                doc_id, exc_info=True,
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
