"""
Orquestador central del pipeline de facturas.

Conecta:
  Fase 2: OCR
  Fase 3: Resolución de campos en paralelo (identidad, fiscal, semántica)
  Fase 4: Cliente destino (secuencial, requiere identidad)
  Fase 5: Ensamblador (lee todos los artefactos)

Uso:
    python -m src.pipeline --folder horeca_sandbox/20_COMPRAS_GASTOS --libro 20_COMPRAS_GASTOS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.logging_config import setup_logging
from src.audit_writer import AuditWriter
from src.phase2_ocr.file_queue_service import (
    run_ocr,
    scan_folder,
)
from src.phase2_ocr.invoice_parser_client import VisionOcrClient
from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model
from src.phase1_splitter import run_split
from src.config import LIBRO_SHORT, settings as get_settings
from src.phase3_identidad_cabecera.main import run_identidad
from src.phase3_fiscal.main import run_fiscal
from src.phase3_semantica.main import run_semantica
from src.phase4_customer.main import run_cliente
from src.phase4_ensamblador.ensamblador import run_ensamblador
from src import state_writer

logger = logging.getLogger("pipeline")

# Mapeo decision_global → status del sidecar `.state.json`.
DECISION_TO_STATUS = {
    "auto": "done",
    "warn": "review",
    "pendiente": "review",
    "block": "blocked",
    "error": "error",
}

# Slug para `numero_factura` al construir el nombre de carpeta renombrado.
import re as _re
import hashlib as _hashlib
_SLUG_RE = _re.compile(r"[^A-Za-z0-9-]+")
_WHITESPACE_RE = _re.compile(r"\s+")


def _normalizar_numero_factura(num: str) -> str:
    """Normaliza el número de factura para el hash fiscal.

    - Uppercase.
    - Colapsa whitespace interno.
    - Elimina ceros a la izquierda en grupos numéricos (``F-0001`` ≡ ``F-1``).
    """
    if not num:
        return ""
    n = _WHITESPACE_RE.sub("", str(num).strip().upper())
    return _re.sub(r"(?<![A-Z0-9])0+(?=[1-9])", "", n)


def _compute_fiscal_hash(nif_emisor: str, numero_factura: str, fecha_expedicion: str) -> str:
    """Hash determinista del triplete identificador fiscal de un asiento.

    Permite detectar la misma factura subida como PDFs distintos. 16 hex
    chars del SHA-256 — suficiente para conjunto operativo de gestoría.
    """
    nif_n = (nif_emisor or "").strip().upper()
    num_n = _normalizar_numero_factura(numero_factura)
    fecha_n = (fecha_expedicion or "").strip()
    key = f"{nif_n}|{num_n}|{fecha_n}"
    return _hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _was_rejected_by_human(folder: Path) -> bool:
    """True si el operario rechazó esta factura via POST /api/invoices/.../action.

    El reject mapea a ``status=review`` + ``action=reject``. Para efectos de
    duplicado fiscal, un asiento rechazado es "cancelado por humano" — no
    debe bloquear nuevas subidas con la misma identidad fiscal.
    """
    try:
        data = state_writer.read(folder)
    except Exception:
        return False
    for ev in data.get("events") or []:
        if ev.get("action") == "reject":
            return True
    return False


def _campo_valor(validacion: dict, nombre: str) -> object:
    """Lee ``campos[nombre].valor_final`` de un resultado_validacion.json."""
    campos = validacion.get("campos") or {}
    return (campos.get(nombre) or {}).get("valor_final")


def _try_rename(folder: Path, libro_short: str, validacion: dict) -> Path:
    """Renombra la carpeta de asiento al esquema descriptivo si procede.

    Reglas (trazabilidad 2.0):
        - Solo si ``decision_global ∈ {auto, warn, pendiente}``.
        - Y si están presentes ``fecha_expedicion``, ``nif_cliente``
          (NIF del cliente de la gestoría, resuelto por phase4_customer)
          y ``numero_factura`` — los tres dentro de
          ``validacion["campos"][nombre]["valor_final"]``.

    En caso contrario devuelve la carpeta sin cambios (señal visual de
    "necesita atención humana").
    """
    decision = validacion.get("decision_global")
    if decision not in {"auto", "warn", "pendiente"}:
        return folder

    fecha = _campo_valor(validacion, "fecha_expedicion")
    nif = _campo_valor(validacion, "nif_cliente")
    num = _campo_valor(validacion, "numero_factura")
    if not all([fecha, nif, num]):
        logger.info(
            "[pipeline] rename omitido (faltan datos): fecha=%s nif_cliente=%s num=%s",
            fecha, nif, num,
        )
        return folder

    num_slug = _SLUG_RE.sub("-", str(num)).strip("-")
    new_name = f"{libro_short}_{fecha}_{nif}_{num_slug}"
    return state_writer.rename(folder, new_name)


@dataclass
class PhaseResult:
    fase: str
    ok: bool
    motivo: str = ""
    duration_ms: float = 0.0


def _finalize_document(
    doc_id: str | None,
    doc_dir: str | None,
    results: list[PhaseResult],
    file_path: str,
    *,
    audit: AuditWriter,
    asientos_root: str,
    libro_short: str,
    summary: dict,
    status_file: str | None,
    processed_offset: int,
) -> None:
    """Reducer serial: bookkeeping ordenado tras process_document.

    Detección de duplicado fiscal, estado del sidecar, rename, auditoría,
    contadores y status_file. DEBE invocarse en serie (un documento a la vez)
    desde el hilo principal: find_by_fiscal_hash y los contadores no son
    seguros bajo concurrencia.
    """
    decision = _leer_decision_global(doc_dir) if doc_id else "error"

    folder_final = Path(doc_dir) if doc_dir else None
    if folder_final and folder_final.exists():
        validacion = _leer_validacion(folder_final)
        status = DECISION_TO_STATUS.get(decision, "error")
        event: dict = {"status": status, "decision": decision}
        motivos = list(validacion.get("motivos_revision") or []) if validacion else []

        fiscal_hash: str | None = None
        if validacion:
            nif_emisor = _campo_valor(validacion, "nif_entidad")
            num_fact = _campo_valor(validacion, "numero_factura")
            fecha_exp = _campo_valor(validacion, "fecha_expedicion")
            if nif_emisor and num_fact and fecha_exp:
                fiscal_hash = _compute_fiscal_hash(
                    str(nif_emisor), str(num_fact), str(fecha_exp)
                )
                try:
                    dup_folder = state_writer.find_by_fiscal_hash(
                        Path(asientos_root), fiscal_hash
                    )
                except Exception:
                    dup_folder = None
                if dup_folder is not None and dup_folder.resolve() != folder_final.resolve():
                    try:
                        dup_status = state_writer.current_status(dup_folder)
                    except Exception:
                        dup_status = None
                    dup_rejected = _was_rejected_by_human(dup_folder)
                    if dup_status not in {"cancelled", "error"} and not dup_rejected:
                        logger.warning(
                            "[pipeline] Duplicado fiscal: doc_id=%s ya existe como %s (status=%s)",
                            doc_id, dup_folder.name, dup_status,
                        )
                        status = "blocked"
                        decision = "block"
                        motivos.append(
                            f"duplicado fiscal: ya existe como '{dup_folder.name}' "
                            f"(estado: {dup_status or 'desconocido'})"
                        )
                        event["duplicate_of"] = dup_folder.name
        event["status"] = status
        event["decision"] = decision
        if motivos:
            event["motivos"] = motivos
        if fiscal_hash:
            event["fiscal_hash"] = fiscal_hash
        try:
            state_writer.append(folder_final, event)
        except Exception:
            logger.error(
                "[pipeline] no se pudo registrar status=%s en sidecar para %s",
                status, doc_id, exc_info=True,
            )

        # NO renombrar carpetas bloqueadas/errored: si f1 y f2 comparten triplete
        # fiscal (caso duplicado), f1 se renombra a `compras_FECHA_NIF_NUM` y
        # f2 quedaría con el MISMO nombre destino → colisión en
        # `state_writer.rename`. Mantener `compras_f2` (genérico) para el
        # bloqueado evita la colisión y deja una pista visual clara: la
        # carpeta con nombre descriptivo es la canónica, la genérica es la
        # bloqueada. Cambio de comportamiento intencionado vs el loop serie
        # previo, que dependía de que `decision_global` en el JSON aún fuera
        # "auto" (el orquestador override a "block" solo en memoria).
        if validacion and decision not in {"block", "error"}:
            try:
                folder_final = _try_rename(folder_final, libro_short, validacion)
            except Exception:
                logger.warning(
                    "[pipeline] rename falló para %s; carpeta queda como %s",
                    doc_id, folder_final.name, exc_info=True,
                )

    audit.write(
        doc_id=doc_id,
        file_path=file_path,
        results=results,
        decision=decision,
        output_base_path=asientos_root,
        folder_name=folder_final.name if folder_final else None,
    )

    if decision == "auto":
        summary["ok"] += 1
    elif decision in ("warn", "pendiente"):
        summary["warn"] += 1
    else:
        summary["error"] += 1

    _log_resumen_documento(doc_id, results, decision)

    if status_file:
        _update_status_file(
            status_file,
            doc_id=doc_id,
            phases=[{"fase": r.fase, "ok": r.ok, "motivo": r.motivo} for r in results],
            processed=processed_offset + summary["ok"] + summary["warn"] + summary["error"],
        )


def process_document(
    file_path: str,
    folder_name: str,
    vision_client: VisionOcrClient,
    gemini_model,
    libro: str,
    output_base_path: str,
) -> tuple[str | None, str | None, list[PhaseResult]]:
    """Ejecuta OCR → Identidad → Ensamblador para un documento.

    Devuelve ``(doc_id, doc_dir, results)``. ``doc_dir`` puede usarse
    para renombrar la carpeta de asiento o leer artefactos posteriores.
    """
    results: list[PhaseResult] = []
    libro_short = LIBRO_SHORT.get(libro, libro)

    # ── Fase 2: OCR ──────────────────────────────────────────────────────
    _t0 = time.monotonic()
    try:
        doc_id, doc_dir, is_valid, motivos = run_ocr(
            file_path,
            folder_name,
            vision_client,
            gemini_model,
            output_base_path,
            libro=libro_short,
        )
        results.append(PhaseResult("ocr", ok=True, duration_ms=(time.monotonic() - _t0) * 1000))
        logger.info(f"[pipeline] OCR completado: {doc_id} (valid={is_valid})")
    except Exception as e:
        logger.error(f"[pipeline] OCR falló para {file_path}: {e}", exc_info=True)
        results.append(PhaseResult("ocr", ok=False, motivo=str(e)))
        # Fallback: registrar el error en un sidecar usando el basename
        # como doc_id si la carpeta se llegó a crear, para mantener trazabilidad.
        basename = os.path.splitext(os.path.basename(file_path))[0]
        fallback_dir = Path(output_base_path) / f"{libro_short}_{basename}"
        try:
            if not fallback_dir.exists():
                fallback_dir.mkdir(parents=True, exist_ok=True)
                state_writer.init(
                    fallback_dir,
                    doc_id=basename,
                    file_origin=os.path.relpath(file_path).replace(os.sep, "/"),
                )
            state_writer.append(
                fallback_dir,
                {"status": "error", "motivo": f"{type(e).__name__}: {e}"},
            )
        except Exception:
            logger.critical(
                "[pipeline] no se pudo registrar evento error en sidecar para %s",
                file_path,
                exc_info=True,
            )
        return None, str(fallback_dir), results  # Sin OCR no tiene sentido continuar

    # ── Fase 3: Resolución de campos (en paralelo) ────────────────────────
    # Los módulos de fase 3 son independientes entre sí:
    # cada uno lee documento_extraido.json (o raw_document_ai.json) y
    # escribe su propio artefacto. Se ejecutan en paralelo por diseño.
    fase3_modulos = {
        "identidad_cabecera": lambda: run_identidad(doc_id, doc_dir, usar_llm=True),
        "fiscal":             lambda: run_fiscal(doc_id, doc_dir),
        "semantica":          lambda: run_semantica(doc_id, doc_dir, libro),
    }

    fase3_resultados: dict[str, tuple[bool, float]] = {}
    FASE3_TIMEOUT = 300  # 5 minutos por módulo
    with ThreadPoolExecutor(max_workers=len(fase3_modulos)) as executor:
        _t_submit = {}
        futuros = {}
        for nombre, fn in fase3_modulos.items():
            _t_submit[nombre] = time.monotonic()
            futuros[executor.submit(fn)] = nombre
        for futuro in as_completed(futuros):
            nombre = futuros[futuro]
            ms = (time.monotonic() - _t_submit[nombre]) * 1000
            try:
                fase3_resultados[nombre] = (futuro.result(timeout=FASE3_TIMEOUT), ms)
            except TimeoutError:
                logger.error(
                    f"[pipeline] Fase 3 '{nombre}' excedió timeout ({FASE3_TIMEOUT}s) para {doc_id}"
                )
                fase3_resultados[nombre] = (False, ms)
            except Exception as e:
                logger.error(
                    f"[pipeline] Fase 3 '{nombre}' lanzó excepción para {doc_id}: {e}",
                    exc_info=True,
                )
                fase3_resultados[nombre] = (False, ms)

    for nombre in fase3_modulos:
        ok, ms = fase3_resultados.get(nombre, (False, 0.0))
        results.append(PhaseResult(
            nombre,
            ok=ok,
            motivo="" if ok else f"error técnico en fase {nombre}",
            duration_ms=ms,
        ))
        if not ok:
            logger.warning(f"[pipeline] {nombre} falló (error técnico) para {doc_id}")

    # ── Fase 4: Cliente destino (secuencial, requiere identidad) ────────
    ok_cliente = False
    _t = time.monotonic()
    if fase3_resultados.get("identidad_cabecera", (False, 0.0))[0]:
        try:
            ok_cliente = run_cliente(doc_id, doc_dir, libro)
        except Exception as e:
            logger.error(
                f"[pipeline] Fase 4 cliente_destino falló para {doc_id}: {e}",
                exc_info=True,
            )
    results.append(PhaseResult(
        "cliente_destino",
        ok=ok_cliente,
        motivo="" if ok_cliente else "error o identidad no disponible",
        duration_ms=(time.monotonic() - _t) * 1000,
    ))
    if not ok_cliente:
        logger.warning(f"[pipeline] cliente_destino falló para {doc_id}")

    # ── Fase 5: Ensamblado ───────────────────────────────────────────────
    ok_ensamblado = False
    _t = time.monotonic()
    try:
        ok_ensamblado = run_ensamblador(doc_id, doc_dir, libro)
    except Exception:
        logger.error(
            "[pipeline] Fase 5 ensamblador lanzo excepcion doc_id=%s", doc_id, exc_info=True
        )
    results.append(PhaseResult(
        "ensamblador",
        ok=ok_ensamblado,
        motivo="" if ok_ensamblado else "error tecnico en fase ensamblador",
        duration_ms=(time.monotonic() - _t) * 1000,
    ))
    if not ok_ensamblado:
        logger.error(f"[pipeline] Ensamblador fallo para {doc_id}", exc_info=True)

    return doc_id, doc_dir, results


def run_pipeline(
    folder_path: str,
    libro: str,
    status_file: str | None = None,
    processed_offset: int = 0,
    cancel_requested: Callable[[], bool] | None = None,
    is_multi_book: bool = False,
) -> dict:
    """
    Procesa todos los archivos de una carpeta.

    Args:
        processed_offset: contador acumulado de docs procesados en libros
            anteriores en la misma ejecución multi-libro. Se suma al contador
            local para que la barra de progreso no regrese entre libros.
        cancel_requested: callable opcional que devuelve True si el usuario
            ha solicitado cancelación. Se consulta entre documentos; si
            devuelve True, el loop se detiene y los documentos pendientes
            se quedan en su estado actual (típicamente ``uploaded``).
        is_multi_book: si True, ``run_pipeline`` no escribe los campos
            ``total``/``processed=0`` ni el ``status=completed`` final del
            ``status_file`` — la API orquestadora (``api/main.py``) los
            gestiona globalmente para que la barra de progreso refleje el
            agregado de todos los libros, no solo el del libro actual.

    Returns:
        {"total": N, "ok": N, "warn": N, "error": N}
    """
    cfg = get_settings()
    summary = {"total": 0, "ok": 0, "warn": 0, "error": 0}

    # Inicializar escritor de auditoría para esta ejecución.
    # Trazabilidad 2.0: el audit JSONL vive bajo `libros/logs/audit/`,
    # no bajo el LOGS_PATH legacy.
    audit = AuditWriter(cfg.audit_path(), libro)

    logger.info(f"[pipeline] Iniciando carpeta={folder_path} libro={libro}")

    # Fase 1: split de PDFs multi-factura (MFP con ADF entrega un único PDF
    # con varias facturas). Idempotente; PDFs de 1 página no llaman a Gemini.
    try:
        split_outcomes = run_split(folder_path)
        n_split = sum(1 for o in split_outcomes if o.status == "split")
        if n_split:
            logger.info(
                "[pipeline] splitter: %d PDF(s) multi-factura divididos en %d facturas",
                n_split,
                sum(o.n_facturas for o in split_outcomes if o.status == "split"),
            )
    except Exception as e:
        # El splitter nunca debe tumbar el pipeline; sin él, cada PDF se
        # procesa como un único asiento (comportamiento previo).
        logger.error(f"[pipeline] splitter falló: {e}", exc_info=True)

    try:
        files = scan_folder(folder_path)
    except Exception as e:
        logger.error(f"[pipeline] No se pudo escanear la carpeta: {e}", exc_info=True)
        if status_file:
            _update_status_file(
                status_file,
                status="error",
                error_message=str(e),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        return summary

    # Idempotencia: si una factura ya tiene un asiento en estado terminal o
    # pendiente de confirmar humano, saltarla. Evita que un nuevo escaneo
    # reprocese facturas anteriores que la UI ya oculta del inbox.
    #
    # Estados que se saltan:
    #   - ``done``             — esperando confirm humano (no reprocesar para no perder revisión)
    #   - ``confirmed``        — asiento contable cerrado
    #   - ``blocked``          — bloqueado por regla (ej. duplicado fiscal)
    #   - ``cancelled``        — cancelado por usuario
    #   - ``error``            — error técnico (re-OCR explícito requiere acción humana via UI)
    #   - ``split``            — original ya dividido, los hijos se procesan por separado
    #   - ``pre_scan_failed``  — pre-scan Fase 1 falló en /upload, requiere retry u override-as-single humano
    asientos_root_for_skip = cfg.asientos_path()
    # Scope por libro: un mismo nombre de fichero en libros distintos son
    # facturas distintas (ej. ``factura.pdf`` en compras vs ventas). Sin
    # scope, ``find_folder_by_doc_id`` devolvería un asiento de OTRO libro y
    # el nuevo upload se silencia sin avisar.
    libro_short_for_skip = LIBRO_SHORT.get(libro, libro)
    pending_files: list[str] = []
    skipped = 0
    for fp in files:
        doc_id = os.path.basename(fp).rsplit(".", 1)[0]
        existing_folder = state_writer.find_folder_by_doc_id(
            asientos_root_for_skip, doc_id, libro_short=libro_short_for_skip
        )
        if existing_folder and (
            state_writer.is_done(existing_folder)
            or state_writer.is_terminal(existing_folder)
        ):
            skipped += 1
            logger.info(
                "[pipeline] Saltando '%s' — asiento ya en estado %s",
                doc_id, state_writer.current_status(existing_folder),
            )
            continue
        pending_files.append(fp)

    if skipped:
        logger.info(f"[pipeline] {skipped} factura(s) ya procesadas — omitidas")
    files = pending_files

    summary["total"] = len(files)
    if not files:
        logger.warning("[pipeline] No se encontraron archivos para procesar")
        # En multi-book NO marcamos completed (la API global lo hace tras el
        # último libro). Sí seguimos limpiando current_file para que el spinner
        # no muestre el archivo del libro anterior.
        if status_file and not is_multi_book:
            _update_status_file(
                status_file,
                status="completed",
                total=0,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        elif status_file:
            _update_status_file(status_file, current_file=None)
        return summary

    if status_file:
        if is_multi_book:
            # Solo actualizamos status="running" y current_file. No pisamos
            # total/processed/started_at (los gestiona la API orquestadora).
            _update_status_file(
                status_file,
                status="running",
                current_file=None,
                error_message=None,
            )
        else:
            _update_status_file(
                status_file,
                status="running",
                total=len(files),
                processed=0,
                current_file=None,
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=None,
                error_message=None,
            )

    # Guardia de reinicio: ".pending_confirm.json" señala que hay un lote en
    # curso a la espera de confirmación humana. POST /api/pipeline/confirm
    # lo elimina al cerrar. Si el contenedor se reinicia, el endpoint
    # /api/pipeline/status lo reporta para que la UI avise al operario.
    pending_doc_ids = [os.path.basename(f).rsplit(".", 1)[0] for f in files]
    # Carry-over de facturas en status=review de runs anteriores: el
    # operario las rechazó previamente y merecen una segunda oportunidad en
    # el nuevo lote (Task 3 del refactor). Si tras este segundo pase vuelve
    # a rechazarlas, el frontend disparará un hard delete.
    pending_doc_ids = _append_review_carryover(cfg.asientos_path(), pending_doc_ids)
    _write_pending_confirm(cfg, pending_doc_ids, merge=is_multi_book)

    vision_client = VisionOcrClient()
    gemini_model = _init_gemini_model()
    folder_name = os.path.basename(os.path.normpath(folder_path))
    libro_short = LIBRO_SHORT.get(libro, libro)
    asientos_root = cfg.asientos_path()

    max_workers = max(1, int(getattr(cfg, "pipeline_max_concurrency", 1)))
    file_iter = iter(files)
    inflight: dict = {}  # future -> file_path
    t_start = time.monotonic()

    def _submit_next(ex) -> bool:
        fp = next(file_iter, None)
        if fp is None:
            return False
        if status_file:
            _update_status_file(status_file, current_file=os.path.basename(fp))
        fut = ex.submit(
            process_document, fp, folder_name, vision_client, gemini_model,
            libro, asientos_root,
        )
        inflight[fut] = fp
        return True

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            cancelled = bool(cancel_requested and cancel_requested())
            if not cancelled:
                for _ in range(max_workers):
                    if not _submit_next(ex):
                        break
            while inflight:
                done, _pending = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    fp = inflight.pop(fut)
                    try:
                        doc_id, doc_dir, results = fut.result()
                    except Exception as e:
                        logger.error(
                            "[pipeline] process_document lanzó excepción para %s: %s",
                            fp, e, exc_info=True,
                        )
                        summary["error"] += 1
                        if status_file:
                            _update_status_file(
                                status_file,
                                processed=processed_offset + summary["ok"] + summary["warn"] + summary["error"],
                            )
                        continue
                    _finalize_document(
                        doc_id, doc_dir, results, fp,
                        audit=audit, asientos_root=asientos_root,
                        libro_short=libro_short, summary=summary,
                        status_file=status_file, processed_offset=processed_offset,
                    )
                if cancel_requested and cancel_requested():
                    if not cancelled:
                        logger.warning(
                            "[pipeline] Cancelación solicitada — no se enviarán más documentos (%d en vuelo)",
                            len(inflight),
                        )
                        cancelled = True
                else:
                    while len(inflight) < max_workers and _submit_next(ex):
                        pass

        summary["elapsed_s"] = round(time.monotonic() - t_start, 2)
        _print_summary(folder_path, summary)

        if status_file and not is_multi_book:
            _update_status_file(
                status_file,
                status="completed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                summary=summary,
            )
    except Exception as e:
        logger.error(f"[pipeline] Error en run_pipeline: {e}", exc_info=True)
        if status_file:
            _update_status_file(
                status_file,
                status="error",
                error_message=str(e),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        raise

    return summary


def _leer_validacion(doc_dir: str | Path) -> dict | None:
    """Lee ``resultado_validacion.json`` de la carpeta de asiento. ``None`` si no existe."""
    path = Path(doc_dir) / "resultado_validacion.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _leer_decision_global(doc_dir: str | Path) -> str:
    """Devuelve ``decision_global`` desde la validación o ``'error'`` si no existe."""
    data = _leer_validacion(doc_dir)
    if not data:
        return "error"
    return data.get("decision_global", "error")


def _log_resumen_documento(doc_id: str | None, results: list[PhaseResult], decision: str) -> None:
    fases = " | ".join(f"{r.fase}={'✓' if r.ok else '✗'}" for r in results)
    logger.info(f"[pipeline] {doc_id or 'UNKNOWN'}: [{fases}] → {decision.upper()}")


def _print_summary(folder: str, summary: dict) -> None:
    print("\n" + "=" * 60)
    print("RESUMEN PIPELINE")
    print("=" * 60)
    print(f"Carpeta:  {folder}")
    print(f"Total:    {summary['total']}")
    print(f"Auto:     {summary['ok']}")
    print(f"Revisión: {summary['warn']}")
    print(f"Error:    {summary['error']}")
    print("=" * 60)


def _append_review_carryover(asientos_root: str | Path, doc_ids: list[str]) -> list[str]:
    """Añade al lote los doc_ids de carpetas con sidecar status=``review``.

    Estas son facturas que el operario rechazó en revisiones previas (la
    transición a ``review`` la dispara el endpoint /api/pipeline/action con
    ``action=reject``). El refactor Tasks 2/3/6 las quiere reaparecer una
    sola vez en el siguiente escaneo: si el operario las vuelve a rechazar,
    el frontend dispara un hard-delete; si las confirma, salen del bucle.

    - Preserva el orden de ``doc_ids`` existente (los nuevos van primero).
    - Carry-overs se añaden ordenados por nombre de carpeta — orden estable
      sin depender del orden de iteración del filesystem.
    - Dedupe por ``doc_id``: si un nuevo upload coincide con un review
      pendiente, sólo aparece una vez (caso re-subida del operario).
    - Tolera sidecars corruptos / ilegibles — los salta con un warning.

    Devuelve una nueva lista (no muta la entrada).
    """
    out = list(doc_ids)
    existing: set[str] = set(out)
    root = Path(asientos_root)
    if not root.exists():
        return out

    candidates: list[tuple[str, str]] = []  # (folder_name, doc_id)
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as e:
        logger.warning("[pipeline] no se pudo escanear asientos para carry-over: %s", e)
        return out

    for child in children:
        if not child.is_dir():
            continue
        try:
            if state_writer.current_status(child) != "review":
                continue
            sidecar = state_writer.read(child)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[pipeline] sidecar ilegible en %s: %s", child.name, e)
            continue
        doc_id = sidecar.get("doc_id")
        if not doc_id or doc_id in existing:
            continue
        candidates.append((child.name, doc_id))

    for folder_name, doc_id in candidates:
        out.append(doc_id)
        existing.add(doc_id)
        logger.info(
            "[pipeline] carry-over review doc_id=%s folder=%s",
            doc_id, folder_name,
        )

    return out


def _write_pending_confirm(cfg, doc_ids: list[str], merge: bool = False) -> None:
    """Marca el lote como pendiente de confirmación humana.

    merge=True (multi-libro): une con los doc_ids ya presentes preservando
    orden y sin duplicar, para que el reviewer vea TODOS los libros del run.
    """
    try:
        runtime_dir = Path(cfg.runtime_path())
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / ".pending_confirm.json"
        if merge and path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    existing = (json.load(f) or {}).get("doc_ids_lote") or []
            except (OSError, json.JSONDecodeError):
                existing = []
            seen = set(existing)
            doc_ids = existing + [d for d in doc_ids if d not in seen]
        payload = {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "doc_ids_lote": doc_ids,
        }
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[pipeline] no se pudo escribir .pending_confirm.json: {e}")


def _update_status_file(status_file: str, **kwargs) -> None:
    """Write/update a JSON status file atomically. Never raises; logs errors."""
    try:
        data = {}
        if os.path.exists(status_file):
            with open(status_file, encoding="utf-8") as f:
                data = json.load(f)
        data.update(kwargs)
        tmp_path = status_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, status_file)
    except Exception:
        logger.debug(f"[pipeline] Failed to update status file {status_file}", exc_info=True)


def main() -> None:
    # Configurar logging UNA vez al inicio — reemplaza basicConfig anterior
    # Se lee cfg para obtener logs_path antes de iniciar cualquier procesamiento
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(description="Pipeline completo de facturas HORECA")
    parser.add_argument("--folder", required=True, help="Carpeta de entrada con facturas")
    parser.add_argument("--libro", required=True, help="Libro contable (e.g., 20_COMPRAS_GASTOS)")
    args = parser.parse_args()

    try:
        run_pipeline(args.folder, args.libro)
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"[pipeline] Error fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
