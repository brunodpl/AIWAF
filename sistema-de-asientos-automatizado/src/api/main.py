"""
FastAPI application for the invoice processing pipeline.

Provides endpoints for:
- Listing processed invoices
- Getting invoice details (validation results)
- Approving/rejecting invoices from the UI
- Triggering pipeline processing
"""

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import urllib.request
import urllib.error

import httpx

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from urllib.parse import quote

from src.config import LIBRO_SHORT, settings
from src import state_writer, final_writer
from src.phase1_splitter import split_single_file, SplitOutcome
from src.phase4_customer.maestro import (
    cargar_maestro,
    guardar_maestro,
    registrar_cliente,
)
import base64
from typing import Any, Dict

# Versión inyectada en build (.env.docker o ENV en docker-compose)
APP_VERSION = os.getenv("AIWAF_VERSION", "0.0.0-dev")
LATEST_VERSION_URL = os.getenv(
    "AIWAF_LATEST_VERSION_URL",
    "https://aiwaf-releases.pages.dev/latest.json",
)
FEEDBACK_WEBHOOK_URL = os.getenv("FEEDBACK_WEBHOOK_URL", "").strip()
GESTORIA_NIF = os.getenv("AIWAF_GESTORIA_NIF", "")
GESTORIA_NOMBRE = os.getenv("AIWAF_GESTORIA_NOMBRE", "")

# Lock de pipeline persistente en disco (sobrevive reinicios).
# Si mtime > LOCK_STALE_SECONDS, se considera huérfano y se libera.
LOCK_STALE_SECONDS = 30 * 60  # 30 minutos

logger = logging.getLogger("pipeline.api")

app = FastAPI(
    title="Pipeline de Asientos Automatizados",
    description="API para gestionar el procesamiento de facturas del pipeline",
    version="1.0.0",
)

# CORS para permitir conexiones desde la interfaz Next.js
# En producción, restringir a origins específicos via variable de entorno
allowed_origins = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3003"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    """Endpoint de health check para Docker."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "pipeline-api",
    }


# ──────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────

class InvoiceAction(BaseModel):
    """Modelo para acciones de aprobación/rechazo."""
    action: str  # "approve" | "reject"
    document_id: str
    notes: Optional[str] = None
    corrections_fields: Optional[dict] = None
    corrections_fiscal_lines: Optional[list] = None


# ──────────────────────────────────────────────────────────
# Input validation helpers
# ──────────────────────────────────────────────────────────

# Whitelist de artefactos permitidos para evitar path traversal
ALLOWED_ARTIFACTS = {
    "raw_document_ai",
    "documento_extraido",
    "resultado_identidad_cabecera",
    "resultado_fiscal",
    "resultado_semantica",
    "resultado_cliente",
    "resultado_validacion",
}

# Extensiones de archivos de factura permitidas
ALLOWED_INVOICE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".bmp"
}

# Mapeo book_id (frontend, hereditario UI) → libro_short (carpeta filesystem)
# y libro_long (parámetro `libro` que aún espera `pipeline.run_pipeline`).
#
# Trazabilidad 2.0: las facturas viven en `libros/facturas/{libro_short}/`.
# El book_id se conserva como contrato hacia el frontend para no romper el
# cliente Next.js — el mapeo se centraliza aquí, sin acoplar nombres por todo
# el código.
BOOK_CONFIGS = {
    "gastos":   {"label": "Libro de Gastos y Compras",   "libro_short": "compras", "libro_long": "20_COMPRAS_GASTOS"},
    "ingresos": {"label": "Libro de Ingresos y Ventas",  "libro_short": "ventas",  "libro_long": "21_VENTAS_INGRESOS"},
    "bienes":   {"label": "Libro de Bienes de Inversión","libro_short": "bienes",  "libro_long": "22_BIENES_INVERSION"},
}

# Reverse: libro_short → book_id (para inferir libro desde folder_name).
_LIBRO_SHORT_TO_BOOK = {cfg["libro_short"]: bid for bid, cfg in BOOK_CONFIGS.items()}


# ──────────────────────────────────────────────────────────
# Simple in-memory cache for list endpoints
# ──────────────────────────────────────────────────────────

_stats_cache: dict = {}
_invoices_cache: dict = {}
_CACHE_TTL = 5  # seconds


def validate_doc_id(doc_id: str) -> None:
    """
    Validar que doc_id es un identificador seguro sin path traversal.

    Acepta alfanuméricos, guiones y guiones bajos (ej: factura_001, FACT-A-2024-001).
    Rechaza path traversal (.. / \) y caracteres especiales peligrosos.
    """
    if not doc_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid document ID: empty string"
        )
    if ".." in doc_id or "/" in doc_id or "\\" in doc_id:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid document ID: path traversal not allowed"
        )


def validate_artifact_name(artifact_name: str) -> None:
    """
    Validar que artifact_name está en la whitelist permitida.

    Previene path traversal y acceso a archivos no autorizados.
    """
    if artifact_name not in ALLOWED_ARTIFACTS:
        allowed = ", ".join(sorted(ALLOWED_ARTIFACTS))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid artifact name: '{artifact_name}'. Allowed: {allowed}"
        )


# ──────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────

def get_asientos_dir() -> Path:
    """Raíz de carpetas de asiento (`libros/asientos/`)."""
    return Path(settings().asientos_path())


def get_runtime_dir() -> Path:
    """Estado transitorio del orquestador (`libros/.runtime/`)."""
    return Path(settings().runtime_path())


def load_json_file(path: Path) -> dict:
    """Cargar un archivo JSON de forma segura."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error loading JSON file {path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


def _scan_asiento_folders() -> list[Path]:
    """Devuelve todas las carpetas de asiento ordenadas por nombre.

    Cada carpeta válida contiene un `.state.json` (si falta, se ignora).
    """
    asientos = get_asientos_dir()
    if not asientos.exists():
        return []
    return sorted(
        (p for p in asientos.iterdir() if p.is_dir() and (p / ".state.json").exists()),
        key=lambda p: p.name,
    )


def _libro_from_folder(folder_name: str) -> Optional[str]:
    """Infiere el libro corto (compras/ventas/bienes) desde el nombre de carpeta."""
    prefix = folder_name.split("_", 1)[0] if "_" in folder_name else folder_name
    return prefix if prefix in _LIBRO_SHORT_TO_BOOK else None


def _book_id_from_folder(folder_name: str) -> Optional[str]:
    """Infiere book_id (gastos/ingresos/bienes) desde el nombre de carpeta."""
    libro = _libro_from_folder(folder_name)
    return _LIBRO_SHORT_TO_BOOK.get(libro) if libro else None


def get_doc_folder(doc_id: str) -> Optional[Path]:
    """Localiza la carpeta de asiento que pertenece a ``doc_id``.

    Lee la cabecera del `.state.json` de cada carpeta hasta encontrar coincidencia.
    """
    return state_writer.find_folder_by_doc_id(get_asientos_dir(), doc_id)


def get_validation_result(folder_or_doc: object) -> Optional[dict]:
    """Lee resultado_validacion.json. Acepta Path (carpeta) o str (doc_id)."""
    folder = folder_or_doc if isinstance(folder_or_doc, Path) else get_doc_folder(str(folder_or_doc))
    if not folder:
        return None
    path = folder / "resultado_validacion.json"
    if not path.exists():
        return None
    return load_json_file(path)


def get_all_artifacts(folder_or_doc: object) -> dict:
    """Obtener todos los artefactos JSON de un documento."""
    folder = folder_or_doc if isinstance(folder_or_doc, Path) else get_doc_folder(str(folder_or_doc))
    if not folder or not folder.exists():
        return {}
    artifacts = {}
    for json_file in folder.glob("*.json"):
        artifacts[json_file.stem] = load_json_file(json_file)
    return artifacts


def get_state(folder_or_doc: object) -> Optional[dict]:
    """Lee `.state.json` (cabecera + eventos). ``None`` si no existe."""
    folder = folder_or_doc if isinstance(folder_or_doc, Path) else get_doc_folder(str(folder_or_doc))
    if not folder:
        return None
    try:
        return state_writer.read(folder)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def current_status_of(folder: Path) -> Optional[str]:
    """Status del último evento de `.state.json`, o ``None``."""
    return state_writer.current_status(folder)


def safe_float(val: object, default: float = 0.0) -> float:
    """
    Convertir un valor a float de forma segura.

    Maneja None (válido en líneas EXENTA per AGENTS.md), strings no numéricos,
    strings con coma decimal española ("1250,50" → 1250.5),
    y tipos incompatibles sin lanzar excepción.
    """
    if val is None:
        return default
    try:
        # Normalizar coma decimal española → punto decimal inglés
        normalized = str(val).replace(",", ".") if isinstance(val, str) else val
        return float(normalized)
    except (ValueError, TypeError):
        return default


def find_invoice_file(doc_id: str) -> Optional[Tuple[Path, str]]:
    """Localiza el PDF/imagen original en `libros/facturas/{libro}/`.

    Trazabilidad 2.0: los PDFs nunca se mueven. La carpeta de asiento
    contiene en su `.state.json` el `file_origin` registrado al iniciar el
    procesado — esa es la fuente de verdad. Si no está disponible, hace
    fallback a buscar `{doc_id}.{ext}` en el inbox del libro inferido por
    el prefijo de la carpeta.
    """
    folder = get_doc_folder(doc_id)
    if not folder:
        return None

    # 1) `.state.json[0].file_origin` es la verdad cuando existe.
    state = get_state(folder)
    if state:
        events = state.get("events") or []
        if events:
            origin = events[0].get("file_origin")
            if origin:
                origin_path = Path(origin)
                if not origin_path.is_absolute():
                    origin_path = Path.cwd() / origin_path
                if origin_path.exists():
                    return origin_path, origin_path.name

    # 2) Fallback: inbox del libro inferido del prefijo de la carpeta.
    libro_short = _libro_from_folder(folder.name)
    if libro_short:
        inbox = Path(settings().inbox_path(libro_short))
        if inbox.exists():
            for ext in ALLOWED_INVOICE_EXTENSIONS:
                cand = inbox / f"{doc_id}{ext}"
                if cand.exists():
                    return cand, cand.name
            # Búsqueda exacta por basename con cualquier extensión (evita
            # colisiones del glob suelto: ej que factura_001_backup.pdf
            # gane a factura_001.pdf).
            for cand in inbox.iterdir():
                if cand.is_file() and cand.suffix.lower() in ALLOWED_INVOICE_EXTENSIONS:
                    if cand.stem == doc_id:
                        return cand, cand.name
            # Último recurso: glob suelto (puede tener falsos positivos).
            for cand in inbox.glob(f"*{doc_id}*"):
                if cand.is_file() and cand.suffix.lower() in ALLOWED_INVOICE_EXTENSIONS:
                    return cand, cand.name
    return None


# ──────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────

@app.get("/api/invoices")
def list_invoices(
    include_done: bool = True,
    include_confirmed: bool = False,
    include_cancelled: bool = False,
    include_split: bool = False,
):
    """
    Listar todas las facturas procesadas con su estado.

    Fuente de verdad del status: ``.state.json`` (último evento). El
    JSON ``resultado_validacion.json`` solo se lee para los campos
    resumen (NIF, número, total, etc.) — no para decidir status.

    Para cada carpeta de asiento devuelve:
    - ``id``        : doc_id estable (basename del PDF original)
    - ``folder_name``: nombre actual de la carpeta (renombrada o provisional)
    - ``libro``     : compras / ventas / bienes (prefijo de la carpeta)
    - ``status``    : último evento del `.state.json` (uploaded/processing/
      review/done/confirmed/blocked/error/retrying)
    - ``duplicate_of`` (opcional): si en algún evento se anotó duplicado fiscal,
      el ``folder_name`` del asiento original al que se atribuye.
    - ``decision_global`` + campos resumen desde `resultado_validacion.json`.

    Query params:
    - ``include_done`` (bool, default True): incluye status=``done`` (esperando
      confirm humano). El reviewer lo desactiva tras confirmar para no
      remostrar facturas que ya pasaron a ``confirmed``.
    - ``include_confirmed`` (bool, default False): incluye status=``confirmed``
      (asiento contable cerrado). El historial lo activa.
    - ``include_cancelled`` (bool, default False): incluye status=``cancelled``
      (soft-deleted por el usuario). Oculto por defecto: una factura cancelada
      no debe aparecer en la cola de revisión. El historial puede activarlo.
    - ``include_split`` (bool, default False): incluye status=``split`` (PDF
      padre dividido por Fase 1 en N facturas individuales). Oculto por defecto:
      la trazabilidad continúa en los doc_ids de ``split_into``, no en el padre.
    """
    now = time.time()
    cache_key = f"data_{int(include_done)}_{int(include_confirmed)}_{int(include_cancelled)}_{int(include_split)}"
    ts_key = f"ts_{int(include_done)}_{int(include_confirmed)}_{int(include_cancelled)}_{int(include_split)}"
    if (
        cache_key in _invoices_cache
        and (now - _invoices_cache.get(ts_key, 0)) < _CACHE_TTL
    ):
        return _invoices_cache[cache_key]

    invoices = []
    for folder in _scan_asiento_folders():
        state = get_state(folder)
        if not state:
            continue

        doc_id = state.get("doc_id") or folder.name
        events = state.get("events") or []
        status = events[-1].get("status") if events else None

        # Filtros por status — antes del I/O de validación.
        if not include_done and status == "done":
            continue
        if not include_confirmed and status == "confirmed":
            continue
        if not include_cancelled and status == "cancelled":
            continue
        if not include_split and status == "split":
            continue

        # Extraer duplicate_of si existe en cualquier evento (anotado por
        # el pipeline al detectar duplicado fiscal).
        duplicate_of = None
        fiscal_hash = None
        for ev in events:
            if ev.get("duplicate_of") and not duplicate_of:
                duplicate_of = ev["duplicate_of"]
            if ev.get("fiscal_hash") and not fiscal_hash:
                fiscal_hash = ev["fiscal_hash"]

        # Saltar carpetas huérfanas (runs interrumpidos sin resultado_validacion).
        # No hay nada que el operario pueda revisar sin datos de validación.
        validation = get_validation_result(folder)
        if not validation:
            continue

        campos = validation.get("campos", {})

        record = {
            "id": doc_id,
            "folder_name": folder.name,
            "libro": _libro_from_folder(folder.name),
            "status": status,
            "decision_global": validation.get("decision_global", "pendiente"),
            "timestamp": validation.get("fecha_ensamblado", ""),
            "nif_entidad": campos.get("nif_entidad", {}).get("valor_final", ""),
            "nombre_entidad": campos.get("nombre_entidad", {}).get("valor_final", ""),
            "numero_factura": campos.get("numero_factura", {}).get("valor_final", ""),
            "total_euros": campos.get("total_euros", {}).get("valor_final", 0),
        }
        if duplicate_of:
            record["duplicate_of"] = duplicate_of
        if fiscal_hash:
            record["fiscal_hash"] = fiscal_hash
        invoices.append(record)

    result = {"invoices": invoices, "total": len(invoices)}
    _invoices_cache[cache_key] = result
    _invoices_cache[ts_key] = now
    return result


@app.get("/api/invoices/{doc_id}")
def get_invoice(doc_id: str):
    """
    Obtener detalles completos de una factura específica.

    Incluye todos los campos validados, desglose fiscal y decisión.
    """
    validate_doc_id(doc_id)

    folder = get_doc_folder(doc_id)
    if not folder:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")
    validation = get_validation_result(folder)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")

    campos = validation.get("campos", {})

    # Extraer líneas fiscales de campos.lineas_fiscales.valor_final
    lineas_fiscales_raw = campos.get("lineas_fiscales", {}).get("valor_final", [])
    fiscal_lines = []
    for idx, linea in enumerate(lineas_fiscales_raw):
        fiscal_lines.append({
            "id": f"line_{idx}",
            "base": safe_float(linea.get("base_euros")),
            "tipo_iva": safe_float(linea.get("tipo_porcentaje")),
            "cuota": safe_float(linea.get("cuota")),
            "total": safe_float(linea.get("total_linea")),
            "clasificacion": linea.get("clasificacion", ""),
            "decision_linea": linea.get("decision_linea", ""),
        })

    inv_file = find_invoice_file(doc_id)
    invoice_filename = inv_file[1] if inv_file else None

    state = get_state(folder) or {}
    events = state.get("events") or []
    status = events[-1].get("status") if events else None

    return {
        "id": doc_id,
        "folder_name": folder.name,
        "libro": _libro_from_folder(folder.name),
        "status": status,
        "decision_global": validation.get("decision_global"),
        "metadata": {
            "fecha_ensamblado": validation.get("fecha_ensamblado", ""),
            "libro": validation.get("libro", ""),
        },
        "fields": {
            "nif_entidad": campos.get("nif_entidad", {}),
            "nombre_entidad": campos.get("nombre_entidad", {}),
            "numero_factura": campos.get("numero_factura", {}),
            "fecha_expedicion": campos.get("fecha_expedicion", {}),
            "fecha_operacion": campos.get("fecha_operacion", {}),
            "total_euros": campos.get("total_euros", {}),
            "nif_receptor": campos.get("nif_receptor", {}),
            "nombre_receptor": campos.get("nombre_receptor", {}),
            "concepto": campos.get("concepto", {}),
            "cuenta_contable": campos.get("cuenta_contable", {}),
            "nif_cliente": campos.get("nif_cliente", {}),
            "nombre_cliente": campos.get("nombre_cliente", {}),
        },
        "fiscal_lines": fiscal_lines,
        "artifacts": list(get_all_artifacts(folder).keys()),
        "invoice_filename": invoice_filename,
        "events": events,  # línea de vida operativa para la UI
    }


@app.get("/api/invoices/{doc_id}/artifacts/{artifact_name}")
def get_artifact(doc_id: str, artifact_name: str):
    """
    Obtener un artefacto específico de un documento.

    Artefactos disponibles: ver ALLOWED_ARTIFACTS.
    """
    validate_doc_id(doc_id)
    validate_artifact_name(artifact_name)

    folder = get_doc_folder(doc_id)
    if not folder:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")
    artifact_path = folder / f"{artifact_name}.json"
    if not artifact_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact {artifact_name} not found for document {doc_id}"
        )
    return load_json_file(artifact_path)


@app.post("/api/invoices/{doc_id}/action")
def process_invoice_action(doc_id: str, action: InvoiceAction):
    """
    Procesar acción de usuario sobre una factura (aprobar/rechazar).

    Trazabilidad 2.0:
    - El PDF NO se mueve (vive permanentemente en `libros/facturas/{libro}/`).
    - La acción se registra como evento en `.state.json` de la carpeta del
      asiento. Mapea ``approve`` → status ``done``, ``reject`` → status
      ``review`` (sigue requiriendo atención humana hasta resolver).
    - Se eliminan los antiguos `action_log.json` y `shutil.move` — toda la
      línea de vida del documento queda en un único sitio.
    """
    validate_doc_id(doc_id)

    if action.action not in {"approve", "reject"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{action.action}'. Allowed: approve, reject",
        )

    folder = get_doc_folder(doc_id)
    if not folder:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")
    validation = get_validation_result(folder)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")

    # Estados desde los que NO se permite aprobar/rechazar:
    #
    # - ``confirmed``  — ya generó asiento contable; cambiarlo requiere flujo
    #                    administrativo separado (revertir la confirmación).
    # - ``cancelled``  — run abandonado; el usuario debe relanzar el pipeline
    #                    antes de poder aprobar/rechazar de nuevo.
    # - ``processing``/``uploaded``/``retrying`` — pipeline aún no completó.
    #
    # Sí se permite desde ``review`` (caso normal), ``done`` (re-revisión),
    # ``blocked`` (override humano del bloqueo del pipeline), ``error``
    # (override de fallo técnico) y ``renamed`` (estado intermedio que no
    # implica nada del workflow).
    current = state_writer.current_status(folder)
    NON_ACTIONABLE = {"confirmed", "cancelled", "processing", "uploaded", "retrying"}
    if current in NON_ACTIONABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Invoice {doc_id} está en estado '{current}' — no se permite "
                "approve/reject. Espera a que el pipeline termine o relánzalo."
            ),
        )

    status = "done" if action.action == "approve" else "review"
    event = {
        "status": status,
        "actor": "user",  # placeholder hasta que haya autenticación
        "action": action.action,
        "decision_original": validation.get("decision_global"),
    }
    if action.notes:
        event["notes"] = action.notes
    if action.corrections_fields:
        event["corrections_fields"] = action.corrections_fields
    if action.corrections_fiscal_lines:
        event["corrections_fiscal_lines"] = action.corrections_fiscal_lines

    try:
        state_writer.append(folder, event)
    except (FileNotFoundError, OSError) as e:
        logger.error("[action] no se pudo escribir evento en sidecar: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error saving action: {e}")

    # Invalidar cachés para reflejar el nuevo status en /api/invoices y /api/stats.
    _invoices_cache.clear()
    _stats_cache.clear()

    return {
        "status": "success",
        "message": f"Action '{action.action}' recorded for invoice {doc_id}",
        "doc_id": doc_id,
        "folder_name": folder.name,
        "new_status": status,
    }


@app.get("/api/stats")
def get_stats():
    """
    Obtener estadísticas generales del pipeline.

    Dos dimensiones independientes:
    - ``by_decision``  : decisión automática del pipeline (auto/warn/pendiente/block)
    - ``by_user_action``: acciones humanas registradas en `.state.json`
                          (approved/rejected — derivadas del campo `action`).
    """
    now = time.time()
    if _stats_cache and (now - _stats_cache.get("ts", 0)) < _CACHE_TTL:
        return _stats_cache["data"]

    folders = _scan_asiento_folders()
    stats = {
        "total": len(folders),
        "by_decision": {"auto": 0, "warn": 0, "pendiente": 0, "block": 0},
        "by_user_action": {"approved": 0, "rejected": 0},
    }

    for folder in folders:
        validation = get_validation_result(folder)
        if validation:
            decision = validation.get("decision_global", "pendiente")
            if decision in stats["by_decision"]:
                stats["by_decision"][decision] += 1

        # Acciones humanas viven en los eventos del sidecar.
        state = get_state(folder) or {}
        for ev in (state.get("events") or []):
            action = ev.get("action")
            if action == "approve":
                stats["by_user_action"]["approved"] += 1
            elif action == "reject":
                stats["by_user_action"]["rejected"] += 1

    _stats_cache["data"] = stats
    _stats_cache["ts"] = now
    return stats


@app.get("/api/invoices/{doc_id}/file")
def get_invoice_file(doc_id: str):
    """
    Servir el archivo de factura original (PDF/imagen) desde PROCESADAS o INCIDENCIAS.

    Returns el archivo para visualización en el frontend.
    """
    validate_doc_id(doc_id)

    result = find_invoice_file(doc_id)

    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"Invoice file not found for document {doc_id}"
        )

    invoice_file, filename = result

    # Determinar el media type según extensión
    ext = invoice_file.suffix.lower()
    media_type_map = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }

    media_type = media_type_map.get(ext, "application/octet-stream")

    # RFC 5987 encoding for filenames with special characters
    encoded_filename = quote(filename)

    return FileResponse(
        path=str(invoice_file),
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}"
        }
    )


# ──────────────────────────────────────────────────────────
# Pipeline lock + cancel flag + status (libros/.runtime/)
# ──────────────────────────────────────────────────────────

def _lock_path() -> Path:
    return get_runtime_dir() / "pipeline.lock"


def _cancel_path() -> Path:
    return get_runtime_dir() / "pipeline.cancel"


def _status_path() -> Path:
    return get_runtime_dir() / "pipeline_status.json"


def is_pipeline_locked() -> bool:
    """
    True si hay un pipeline corriendo. Si el lock es viejo (>30min) lo libera
    automáticamente — protege contra crashes que dejen el lock pegado.
    """
    lock = _lock_path()
    if not lock.exists():
        return False
    try:
        age = time.time() - lock.stat().st_mtime
        if age > LOCK_STALE_SECONDS:
            logger.warning(
                "[pipeline-lock] Lock huérfano (%.0fs) — liberando automáticamente", age
            )
            lock.unlink(missing_ok=True)
            return False
    except OSError as exc:
        logger.error("[pipeline-lock] No se puede inspeccionar el lock: %s", exc)
        return False
    return True


def acquire_pipeline_lock() -> bool:
    """Crea el fichero de lock. Devuelve False si ya existía y está vivo."""
    if is_pipeline_locked():
        return False
    lock = _lock_path()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps({"acquired_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}),
            encoding="utf-8",
        )
        # Limpiar flag de cancelación previa
        _cancel_path().unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.error("[pipeline-lock] No se pudo crear el lock: %s", exc)
        return False


def release_pipeline_lock() -> None:
    try:
        _lock_path().unlink(missing_ok=True)
        _cancel_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("[pipeline-lock] Error liberando lock: %s", exc)


def is_cancel_requested() -> bool:
    return _cancel_path().exists()


@app.on_event("startup")
def _recover_stale_pipeline_state() -> None:
    """
    Al arrancar el contenedor, si quedó un lock huérfano + status 'running',
    los marcamos como interrumpidos para que la UI no se quede esperando.
    """
    try:
        status_path = _status_path()
        if not status_path.exists():
            _lock_path().unlink(missing_ok=True)
            _cancel_path().unlink(missing_ok=True)
            return

        if not is_pipeline_locked():
            with status_path.open("r", encoding="utf-8") as f:
                status = json.load(f)
            if status.get("status") == "running":
                logger.warning("[startup] Pipeline 'running' sin lock vivo → marcando como interrumpido")
                status["status"] = "error"
                status["error_message"] = "Procesamiento interrumpido por reinicio del sistema"
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                with status_path.open("w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
        _cancel_path().unlink(missing_ok=True)

        # Detectar pending_confirm huérfano (lote pipeline OK pero sin
        # confirmación humana antes del reinicio). No lo borramos: la UI lo
        # mostrará al operario para que confirme cuando vuelva.
        pending = _load_pending_confirm()
        if pending:
            logger.warning(
                "[startup] .pending_confirm.json detectado: %d facturas a la "
                "espera de confirmación humana — UI las mostrará en /api/pipeline/status",
                len(pending.get("doc_ids_lote", [])),
            )
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[startup] Error en recovery de pipeline: %s", exc, exc_info=True)


# ──────────────────────────────────────────────────────────
# New endpoints: books, upload, pipeline, clients
# ──────────────────────────────────────────────────────────

def _inbox_for_book(book_id: str) -> Path:
    """Inbox permanente del libro (`libros/facturas/{libro_short}/`)."""
    libro_short = BOOK_CONFIGS[book_id]["libro_short"]
    return Path(settings().inbox_path(libro_short))


# ── Pre-scan síncrono en /upload ───────────────────────────────────────
#
# split_single_file() es síncrono (CPU + I/O Gemini), así que se ejecuta en
# threads vía asyncio.to_thread(). Un semáforo limita la concurrencia para
# no saturar quota Gemini Vision.
#
# El cliente Gemini se inicializa una sola vez por proceso y se cachea en
# `app.state` para no pagar el coste en cada upload de imagen pura.

def _build_prescan_skipped(file_entry: dict) -> dict:
    """Pre-scan para archivos no-PDF (imagen): siempre 1 factura."""
    return {
        "status": "skipped_non_pdf",
        "n_pages": 1,
        "detected_invoices": 1,
        "children": [],
        "error": None,
    }


def _prescan_outcome_to_dict(
    outcome: SplitOutcome,
    *,
    asiento_folder: Optional[Path],
) -> dict:
    """Convierte un ``SplitOutcome`` en el sub-objeto ``pre_scan`` de la
    respuesta del upload.

    Para ``status="split"``, los hijos se leen del propio sidecar
    (``get_split_children_status``) — el splitter ya los creó.
    """
    n_pages = outcome.n_pages or (1 if outcome.status == "single_page" else 0)

    if outcome.status == "split":
        children_raw: list[dict] = []
        if asiento_folder is not None:
            try:
                children_raw = state_writer.get_split_children_status(asiento_folder)
            except Exception:
                children_raw = []
        return {
            "status": "split",
            "n_pages": n_pages,
            "detected_invoices": outcome.n_facturas or len(children_raw),
            "children": children_raw,
            "error": None,
        }

    if outcome.status in ("single_page", "single_factura"):
        return {
            "status": "single",
            "n_pages": n_pages,
            "detected_invoices": 1,
            "children": [],
            "error": None,
        }

    # status == "error" → tras agotar reintentos
    return {
        "status": "failed",
        "n_pages": n_pages,
        "detected_invoices": 0,
        "children": [],
        "error": {
            "kind": "gemini_pre_scan_failed",
            "message": outcome.error or "unknown",
            "attempts": int(settings().prescan_max_attempts),
        },
    }


def _record_prescan_failure(
    asiento_folder: Path, outcome: SplitOutcome, *, model_used: Optional[str],
) -> None:
    """Append `pre_scan_failed` al sidecar tras agotar reintentos.

    Best-effort: cualquier fallo del state writer se loggea pero no
    interrumpe el upload (el archivo ya está en disco con sidecar
    ``uploaded``; el siguiente run del operario lo detectará como
    pendiente sin pre-scan registrado).
    """
    try:
        state_writer.append(asiento_folder, {
            "status": "pre_scan_failed",
            "error_kind": "gemini_pre_scan_failed",
            "last_error": outcome.error or "unknown",
            "attempts": int(settings().prescan_max_attempts),
            "model_used": model_used or settings().gemini_ocr_model,
        })
    except Exception:
        logger.warning(
            "[prescan] no se pudo registrar pre_scan_failed en sidecar %s",
            asiento_folder.name, exc_info=True,
        )


async def _run_prescan_for_uploads(
    verified_files: list[dict],
    *,
    book_id: str,
    upload_dir: Path,
    asientos_root: Path,
) -> dict:
    """Ejecuta el splitter Fase 1 sobre cada archivo recién subido.

    Mutates ``verified_files`` añadiendo ``pre_scan`` a cada entrada.

    Devuelve el agregado ``detected_summary`` con totales para la UI.
    """
    cfg = settings()
    summary = {"total_invoices": 0, "files_ok": 0, "files_blocked": 0, "files_skipped": 0}

    if not cfg.prescan_enabled:
        # Feature flag off: comportamiento legacy (splitter solo en pipeline.run).
        # Marcamos cada PDF como "skipped" para que la UI no se confunda — el
        # frontend tratará esto como "todavía no conocemos nº facturas".
        for fe in verified_files:
            if fe.get("status") == "uploaded":
                fe["pre_scan"] = {
                    "status": "skipped_disabled",
                    "n_pages": 0,
                    "detected_invoices": 1,  # supuesto optimista, igual que hoy
                    "children": [],
                    "error": None,
                }
                summary["total_invoices"] += 1
                summary["files_skipped"] += 1
        return summary

    # Cliente Gemini cacheado por proceso: lazy init en el primer PDF que
    # llegue. Imágenes no lo necesitan. Si ya hay uno en app.state, lo
    # reusamos sin tocar.
    gemini_client = getattr(app.state, "gemini_client", None)

    # Lock para que el lazy init sea seguro frente a uploads concurrentes.
    init_lock: asyncio.Lock = getattr(app.state, "_gemini_init_lock", None)
    if init_lock is None:
        init_lock = asyncio.Lock()
        app.state._gemini_init_lock = init_lock

    semaphore = asyncio.Semaphore(max(1, int(cfg.prescan_max_concurrency)))

    async def _do_one(file_entry: dict) -> None:
        """Procesa un único archivo. Mutates ``file_entry["pre_scan"]``."""
        nonlocal gemini_client
        name = file_entry["name"]
        sidecar_status = file_entry.get("status")
        folder_name = file_entry.get("folder_name")

        # Saltar archivos que ni siquiera tienen sidecar (init falló antes).
        if sidecar_status != "uploaded" or folder_name is None:
            return

        ext = os.path.splitext(name)[1].lower()
        if ext != ".pdf":
            # Imagen → siempre 1 factura. Sin coste Gemini.
            file_entry["pre_scan"] = _build_prescan_skipped(file_entry)
            summary["files_skipped"] += 1
            summary["total_invoices"] += 1
            return

        # Lazy init del cliente Gemini (una vez por proceso).
        if gemini_client is None:
            async with init_lock:
                if getattr(app.state, "gemini_client", None) is None:
                    try:
                        from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model
                        app.state.gemini_client = await asyncio.to_thread(_init_gemini_model)
                    except Exception as exc:
                        # Si el init falla, marcamos el archivo como pre_scan_failed
                        # con error explícito. El operario verá el bloqueo.
                        logger.error("[prescan] no se pudo inicializar Gemini: %s", exc, exc_info=True)
                        asiento_folder = asientos_root / folder_name
                        _record_prescan_failure(
                            asiento_folder,
                            SplitOutcome(source=name, status="error", error=f"gemini_init: {exc}"),
                            model_used=None,
                        )
                        file_entry["pre_scan"] = {
                            "status": "failed",
                            "n_pages": 0,
                            "detected_invoices": 0,
                            "children": [],
                            "error": {
                                "kind": "gemini_init_failed",
                                "message": str(exc),
                                "attempts": 1,
                            },
                        }
                        summary["files_blocked"] += 1
                        return
                gemini_client = app.state.gemini_client

        pdf_path = upload_dir / name
        asiento_folder = asientos_root / folder_name

        # Timeout duro por archivo: si Gemini cuelga, no bloqueamos el upload
        # entero indefinidamente. El split_single_file ya hace sus reintentos
        # internos con sleep; el timeout aquí es la guarda externa.
        timeout_s = max(5, int(cfg.prescan_timeout_seconds))

        async with semaphore:
            try:
                outcome: SplitOutcome = await asyncio.wait_for(
                    asyncio.to_thread(
                        split_single_file,
                        pdf_path,
                        client=gemini_client,
                        model=None,  # usa default de cfg.gemini_ocr_model
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                outcome = SplitOutcome(
                    source=str(pdf_path), status="error",
                    error=f"timeout after {timeout_s}s",
                )
            except Exception as exc:
                outcome = SplitOutcome(
                    source=str(pdf_path), status="error",
                    error=f"unhandled: {exc}",
                )

        if outcome.status == "error":
            _record_prescan_failure(asiento_folder, outcome, model_used=cfg.gemini_ocr_model)
            file_entry["pre_scan"] = _prescan_outcome_to_dict(outcome, asiento_folder=asiento_folder)
            summary["files_blocked"] += 1
        elif outcome.status == "split":
            file_entry["pre_scan"] = _prescan_outcome_to_dict(outcome, asiento_folder=asiento_folder)
            n = file_entry["pre_scan"]["detected_invoices"]
            summary["files_ok"] += 1
            summary["total_invoices"] += n
        else:  # single_page / single_factura
            file_entry["pre_scan"] = _prescan_outcome_to_dict(outcome, asiento_folder=asiento_folder)
            summary["files_ok"] += 1
            summary["total_invoices"] += 1

    # Lanzamos todos los pre-scans en paralelo. El semáforo bornará la
    # concurrencia efectiva. Excepciones se reportan archivo a archivo.
    await asyncio.gather(*(_do_one(fe) for fe in verified_files), return_exceptions=False)
    return summary


@app.get("/api/books")
def list_books():
    """Lista los libros y sus facturas en el inbox permanente.

    Trazabilidad 2.0: las facturas viven en `libros/facturas/{libro}/` y NO se
    mueven aunque hayan sido procesadas. El campo ``files`` lista cada PDF
    junto con el ``status`` de su asiento (si existe), para que la UI pueda
    distinguir "subida nueva" de "ya procesada".
    """
    # Construir índice doc_id → status leyendo los sidecars una sola vez.
    status_by_doc_id: dict[str, str] = {}
    folder_by_doc_id: dict[str, str] = {}
    for folder in _scan_asiento_folders():
        st = get_state(folder) or {}
        doc_id = st.get("doc_id")
        events = st.get("events") or []
        if doc_id and events:
            status_by_doc_id[doc_id] = events[-1].get("status", "")
            folder_by_doc_id[doc_id] = folder.name

    books = []
    for book_id, cfg_book in BOOK_CONFIGS.items():
        inbox = _inbox_for_book(book_id)
        files = []
        if inbox.is_dir():
            for entry in os.scandir(inbox):
                if not entry.is_file():
                    continue
                stat = entry.stat()
                doc_id = os.path.splitext(entry.name)[0]
                files.append({
                    "name": entry.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "added": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "status": status_by_doc_id.get(doc_id),
                    "folder_name": folder_by_doc_id.get(doc_id),
                })
        books.append({
            "id": book_id,
            "label": cfg_book["label"],
            "libro_short": cfg_book["libro_short"],
            "folder": str(inbox),
            "files": sorted(files, key=lambda f: f["added"]),
        })
    return {"books": books}


@app.post("/api/books/{book_id}/upload")
async def upload_files(book_id: str, files: List[UploadFile] = File(...)):
    """Sube facturas al inbox permanente del libro (`libros/facturas/{libro}/`).

    No hay paso intermedio en `PENDIENTES/` — el inbox y la "bandeja de entrada"
    son el mismo sitio (trazabilidad 2.0).

    Rechaza con 409 si hay un pipeline en curso: el splitter Fase 1 corre al
    inicio del run y un PDF subido mid-run no entraría en la corrida actual,
    confundiendo al operario sobre si "se procesó o no".
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}. Valid: {list(BOOK_CONFIGS.keys())}")

    if is_pipeline_locked():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pipeline_running",
                "message": "Hay un procesado en curso. Espera a que termine para subir nuevas facturas.",
            },
        )

    upload_dir = _inbox_for_book(book_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    asientos_root = get_asientos_dir()
    libro_short = LIBRO_SHORT.get(BOOK_CONFIGS[book_id]["libro_short"], BOOK_CONFIGS[book_id]["libro_short"])

    uploaded = []
    errors = []
    # Info de PDFs ya conocidos cuyos hijos (o ellos mismos) siguen requiriendo
    # atención del usuario. Distinto de ``errors``: no es un rechazo técnico,
    # es información útil para que el frontend redirija al reviewer.
    already_processed: list[dict] = []
    # Track de carpetas de asiento creadas durante este upload para no
    # re-disparar init_uploaded por colisión con sidecars recién creados.
    new_sidecars: list[tuple[Path, str, str, int]] = []

    for file in files:
        if not file.filename:
            errors.append({"file": "unknown", "error": "No filename"})
            continue

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_INVOICE_EXTENSIONS:
            errors.append({"file": file.filename, "error": f"Extension not allowed: {ext}"})
            continue

        # Sanitize filename to prevent path traversal
        safe_name = pathlib.PurePosixPath(file.filename).name
        if not safe_name or safe_name.startswith(".") or ".." in safe_name:
            errors.append({"file": file.filename, "error": "Invalid filename"})
            continue

        # Leer contenido y computar SHA-256 ANTES de escribir, para poder
        # rechazar duplicados físicos sin tocar disco.
        try:
            content = await file.read()
        except Exception as e:
            errors.append({"file": file.filename, "error": f"read error: {e}"})
            continue
        sha256_file = hashlib.sha256(content).hexdigest()

        # Duplicado físico: si ya existe un sidecar con este sha256, rechazar
        # salvo que el doc anterior esté en estado ``error`` (retry legítimo).
        try:
            existing = state_writer.find_by_file_sha256(asientos_root, sha256_file)
        except Exception:
            existing = None
        if existing is not None:
            try:
                prev_status = state_writer.current_status(existing)
            except Exception:
                prev_status = None
            # ``error``, ``cancelled`` y ``pre_scan_failed`` permiten retry
            # legítimo: el usuario vuelve a subir el mismo PDF tras un fallo o
            # tras haberlo soft-deletado, y queremos comportamiento "como si
            # no existiera". ``pre_scan_failed`` además ofrece el endpoint
            # dedicado /retry-prescan, pero el re-upload también debe funcionar.
            if prev_status not in ("error", "cancelled", "pre_scan_failed"):
                # Estados "cerrados" — no requieren acción del usuario.
                # ``done`` = lista para confirmar (terminal para revisión).
                CLOSED_STATUSES = {"done", "confirmed", "cancelled"}
                pending_children: list[dict] = []
                if prev_status == "split":
                    try:
                        children = state_writer.get_split_children_status(existing)
                    except Exception:
                        children = []
                    pending_children = [
                        c for c in children if c["status"] not in CLOSED_STATUSES
                    ]
                elif prev_status in {"review", "blocked", "processing", "retrying", "uploaded"}:
                    # PDF de factura única todavía en cola — el propio padre
                    # es el "hijo" pendiente.
                    doc_id_only = existing.name.split("_", 1)[-1]
                    pending_children = [{
                        "doc_id": doc_id_only,
                        "folder_name": existing.name,
                        "status": prev_status,
                    }]

                if pending_children:
                    already_processed.append({
                        "file": file.filename,
                        "original": existing.name,
                        "original_status": prev_status,
                        "pending": pending_children,
                    })
                    logger.info(
                        "[upload] dup con pendientes file=%s sha256=%s existing=%s status=%s pending=%d",
                        file.filename, sha256_file[:12], existing.name, prev_status, len(pending_children),
                    )
                else:
                    errors.append({
                        "file": file.filename,
                        "error": (
                            f"duplicate: el PDF ya está completamente procesado "
                            f"como '{existing.name}'"
                        ),
                        "duplicate_of": existing.name,
                        "duplicate_status": prev_status,
                    })
                    logger.info(
                        "[upload] dup físico rechazado file=%s sha256=%s existing=%s status=%s",
                        file.filename, sha256_file[:12], existing.name, prev_status,
                    )
                continue

        dest = upload_dir / safe_name
        # Colisión de nombre:
        # - Si existe un sidecar activo para este nombre, mantenemos sufijo
        #   timestamp para no pisar (NO debería pasar — la dup física por sha256
        #   ya habría rechazado contenido idéntico; sería colisión de nombre con
        #   contenido distinto).
        # - Si el fichero existe sin sidecar (huérfano de un reset previo), lo
        #   sobreescribimos silenciosamente — no hay asiento que dependa de él.
        if dest.exists():
            doc_id_collision = os.path.splitext(safe_name)[0]
            folder_collision = asientos_root / f"{libro_short}_{doc_id_collision}"
            has_sidecar = (folder_collision / state_writer.SIDECAR_NAME).exists()
            if has_sidecar:
                base, ext_part = os.path.splitext(safe_name)
                ts = int(datetime.now(timezone.utc).timestamp())
                safe_name = f"{base}_{ts}{ext_part}"
                dest = upload_dir / safe_name
            # else: huérfano — overwrite limpio sin renombrar

        try:
            with open(dest, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            uploaded.append(safe_name)
            # Apuntar para init_uploaded post-fsync (al final del loop, tras
            # haber confirmado visibilidad en filesystem).
            doc_id = os.path.splitext(safe_name)[0]
            folder_name = f"{libro_short}_{doc_id}"
            asiento_folder = asientos_root / folder_name
            new_sidecars.append((asiento_folder, doc_id, sha256_file, len(content)))
            # Pequeña pausa entre archivos: en Docker con volúmenes
            # Windows el filesystem puede no estar consistente al
            # instante siguiente — damos margen al SO para sincronizar.
            await asyncio.sleep(0.2)
        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)})

    # Forzar sync global del filesystem al terminar el lote,
    # para que el subsiguiente GET /api/books vea todos los archivos.
    os.sync()

    # Verificar que cada archivo es visible en el filesystem.
    # En Docker Desktop + Windows con bind-mounts, el virtiofs/9p
    # puede tardar hasta 2s en hacer visible un archivo nuevo a
    # os.scandir() incluso tras fsync+sync.
    VERIFY_TIMEOUT = 3.0
    VERIFY_POLL = 0.25
    verified_files = []
    for name in uploaded:
        dest = upload_dir / name
        deadline = time.monotonic() + VERIFY_TIMEOUT
        visible = False
        while time.monotonic() < deadline:
            if dest.exists() and dest.is_file():
                visible = True
                break
            await asyncio.sleep(VERIFY_POLL)
        if visible:
            stat = dest.stat()
            # Crear sidecar .state.json con primer evento ``uploaded``.
            # Tomamos los metadatos asociados a este nombre desde
            # ``new_sidecars`` (mismo orden que ``uploaded``).
            doc_id = os.path.splitext(name)[0]
            sidecar_status: Optional[str] = None
            folder_name: Optional[str] = None
            meta = next((m for m in new_sidecars if m[1] == doc_id), None)
            if meta is not None:
                asiento_folder, _, sha_meta, size_meta = meta
                try:
                    state_writer.init_uploaded(
                        asiento_folder,
                        doc_id=doc_id,
                        file_origin=str(dest.resolve()),
                        sha256_file=sha_meta,
                        size_bytes=size_meta,
                    )
                    sidecar_status = "uploaded"
                    folder_name = asiento_folder.name
                except FileExistsError:
                    # Sidecar ya existe. Dos escenarios:
                    # 1. Retry legítimo / doble-click del frontend: nuestro propio
                    #    sidecar con mismo sha256 (la dup-detection lo habría
                    #    pillado, salvo que estuviera ``error``). Idempotente.
                    # 2. Race con otro upload concurrente que creó el sidecar
                    #    entre nuestro find_by_file_sha256 y este init: el sidecar
                    #    pertenece a OTRO contenido. Reportar como conflicto.
                    folder_name = asiento_folder.name
                    sidecar_status = None
                    try:
                        existing_state = state_writer.read(asiento_folder)
                        existing_sha = None
                        for ev in existing_state.get("events") or []:
                            if ev.get("status") == "uploaded" and ev.get("sha256_file"):
                                existing_sha = ev["sha256_file"]
                                break
                        sidecar_status = state_writer.current_status(asiento_folder)
                        if existing_sha and existing_sha != sha_meta:
                            # Race con upload concurrente de OTRO contenido.
                            logger.warning(
                                "[upload] race detected doc_id=%s — sidecar existente "
                                "tiene sha256=%s, nuestro upload tenía sha256=%s",
                                doc_id, existing_sha[:12], sha_meta[:12],
                            )
                            errors.append({
                                "file": name,
                                "error": (
                                    "conflicto con upload concurrente — el sidecar fue creado "
                                    "por otra subida con contenido distinto. Reintenta."
                                ),
                                "stage": "upload_race",
                            })
                            sidecar_status = None  # no anunciarlo como "uploaded"
                    except Exception:
                        # Lectura del sidecar falló — degradar a "uploaded" defensive.
                        sidecar_status = sidecar_status or "uploaded"
                except Exception as exc:
                    logger.error(
                        "[upload] init_uploaded falló doc_id=%s: %s",
                        doc_id, exc, exc_info=True,
                    )
                    errors.append({"file": name, "error": f"sidecar init failed: {exc}"})

            verified_files.append({
                "name": name,
                "size_kb": round(stat.st_size / 1024, 1),
                "added": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "status": sidecar_status,
                "folder_name": folder_name,
            })
            logger.info(
                "upload_verified file=%s book=%s status=%s",
                name, book_id, sidecar_status,
            )
        else:
            logger.warning("upload_stale file=%s book=%s — not visible after %.1fs", name, book_id, VERIFY_TIMEOUT)
            errors.append({"file": name, "error": f"File saved but not visible after {VERIFY_TIMEOUT:.0f}s — retry or refresh"})

    # Pre-scan síncrono Fase 1: para cada PDF recién subido, llamar al
    # splitter Gemini para detectar si contiene varias facturas. La UI usará
    # `detected_summary.total_invoices` para mostrar el nº real de facturas
    # antes de pulsar "Escanear", y `pre_scan.status="failed"` para bloquear
    # archivos que requieren acción humana (retry / override-as-single).
    detected_summary = {"total_invoices": 0, "files_ok": 0, "files_blocked": 0, "files_skipped": 0}
    try:
        detected_summary = await _run_prescan_for_uploads(
            verified_files,
            book_id=book_id,
            upload_dir=upload_dir,
            asientos_root=asientos_root,
        )
    except Exception as exc:
        # Defensivo: si el pre-scan entero revienta (no debería — cada archivo
        # ya tiene su try/except), el upload no se pierde. La UI lo verá como
        # "sin pre_scan" y se comportará como antes del cambio.
        logger.error("[upload] pre-scan global falló: %s", exc, exc_info=True)

    return {
        "uploaded": uploaded,
        "errors": errors,
        "already_processed": already_processed,
        "files": verified_files,
        "detected_summary": detected_summary,
    }


class _OverridePrescanBody(BaseModel):
    as_single: bool = True


@app.post("/api/books/{book_id}/files/{doc_id}/retry-prescan")
async def retry_prescan(book_id: str, doc_id: str):
    """Reintenta el pre-scan Fase 1 sobre un archivo bloqueado.

    Sólo aplica a archivos cuyo sidecar está en ``pre_scan_failed``. Si Gemini
    Vision vuelve a fallar, se registra otro evento ``pre_scan_failed`` con
    los nuevos detalles. Si tiene éxito, el sidecar termina en ``split`` (si
    detecta N facturas, con N sidecars hijo nuevos) o en ``uploaded`` (si
    descubre que es factura única — el caller debe transitionar manualmente).
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}")

    if is_pipeline_locked():
        raise HTTPException(
            status_code=409,
            detail={"code": "pipeline_running", "message": "Pipeline en curso."},
        )

    safe_doc_id = pathlib.PurePosixPath(doc_id).name
    if not safe_doc_id or safe_doc_id.startswith(".") or ".." in safe_doc_id:
        raise HTTPException(status_code=400, detail="Invalid doc_id")

    libro_short = BOOK_CONFIGS[book_id]["libro_short"]
    asientos_root = get_asientos_dir()
    asiento_folder = asientos_root / f"{libro_short}_{safe_doc_id}"
    if not asiento_folder.exists():
        raise HTTPException(status_code=404, detail=f"Asiento no encontrado: {safe_doc_id}")

    try:
        current = state_writer.current_status(asiento_folder)
    except Exception:
        current = None
    if current != "pre_scan_failed":
        raise HTTPException(
            status_code=400,
            detail=f"El archivo no está en pre_scan_failed (estado actual: {current})",
        )

    # El PDF original sigue en el inbox: el splitter no archivó nada porque
    # falló antes de la decisión "split vs single".
    inbox = _inbox_for_book(book_id)
    pdf_path = inbox / f"{safe_doc_id}.pdf"
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"PDF no encontrado en inbox: {safe_doc_id}.pdf")

    # Antes de re-llamar al splitter, dejamos el sidecar en `retrying` para
    # cumplir la transición permitida pre_scan_failed→retrying y dar trazabilidad
    # del intento. Si split_single_file tiene éxito como split, el propio
    # _apply_split sobreescribirá el sidecar con `split`; si vuelve a fallar,
    # nuestro append posterior pondrá `pre_scan_failed` (transición retrying→
    # pre_scan_failed no está explícita en VALID_TRANSITIONS — caerá en WARN
    # sin romper el sidecar).
    try:
        state_writer.append(asiento_folder, {"status": "retrying", "reason": "retry_prescan"})
    except Exception:
        logger.warning("[retry-prescan] no se pudo marcar retrying", exc_info=True)

    gemini_client = getattr(app.state, "gemini_client", None)
    if gemini_client is None:
        try:
            from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model
            gemini_client = await asyncio.to_thread(_init_gemini_model)
            app.state.gemini_client = gemini_client
        except Exception as exc:
            outcome = SplitOutcome(source=str(pdf_path), status="error", error=f"gemini_init: {exc}")
            _record_prescan_failure(asiento_folder, outcome, model_used=None)
            return {"doc_id": safe_doc_id, "pre_scan": _prescan_outcome_to_dict(outcome, asiento_folder=asiento_folder)}

    cfg = settings()
    timeout_s = max(5, int(cfg.prescan_timeout_seconds))
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                split_single_file, pdf_path, client=gemini_client, model=None,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        outcome = SplitOutcome(source=str(pdf_path), status="error", error=f"timeout after {timeout_s}s")
    except Exception as exc:
        outcome = SplitOutcome(source=str(pdf_path), status="error", error=f"unhandled: {exc}")

    if outcome.status == "error":
        _record_prescan_failure(asiento_folder, outcome, model_used=cfg.gemini_ocr_model)
    elif outcome.status in ("single_page", "single_factura"):
        # Éxito como factura única: dejamos el sidecar en `uploaded` para que
        # el pipeline lo procese normalmente.
        try:
            state_writer.append(asiento_folder, {
                "status": "uploaded",
                "reason": "prescan_retry_single",
                "file_origin": str(pdf_path.resolve()),
            })
        except Exception:
            logger.warning("[retry-prescan] no se pudo transitionar a uploaded", exc_info=True)
    # outcome.status == "split" → split_single_file ya creó los hijos y marcó
    # el sidecar original como `split`. Nada más que hacer.

    return {
        "doc_id": safe_doc_id,
        "pre_scan": _prescan_outcome_to_dict(outcome, asiento_folder=asiento_folder),
    }


@app.post("/api/books/{book_id}/files/{doc_id}/override-prescan")
def override_prescan(book_id: str, doc_id: str, body: _OverridePrescanBody):
    """Fuerza al pipeline a tratar un archivo bloqueado como factura única.

    Útil cuando Gemini Vision está caído pero el operario sabe (mirando el
    PDF) que es 1 sola factura. Tras este override, el archivo sigue su
    flujo normal por el pipeline OCR — no hay re-scan.

    Sólo permite el override desde ``pre_scan_failed`` y solo con
    ``as_single=true``. Cualquier otra cosa devuelve 400.
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}")
    if not body.as_single:
        raise HTTPException(status_code=400, detail="Sólo se soporta as_single=true por ahora")

    if is_pipeline_locked():
        raise HTTPException(
            status_code=409,
            detail={"code": "pipeline_running", "message": "Pipeline en curso."},
        )

    safe_doc_id = pathlib.PurePosixPath(doc_id).name
    if not safe_doc_id or safe_doc_id.startswith(".") or ".." in safe_doc_id:
        raise HTTPException(status_code=400, detail="Invalid doc_id")

    libro_short = BOOK_CONFIGS[book_id]["libro_short"]
    asiento_folder = get_asientos_dir() / f"{libro_short}_{safe_doc_id}"
    if not asiento_folder.exists():
        raise HTTPException(status_code=404, detail=f"Asiento no encontrado: {safe_doc_id}")

    try:
        current = state_writer.current_status(asiento_folder)
    except Exception:
        current = None
    if current != "pre_scan_failed":
        raise HTTPException(
            status_code=400,
            detail=f"El archivo no está en pre_scan_failed (estado actual: {current})",
        )

    inbox = _inbox_for_book(book_id)
    pdf_path = inbox / f"{safe_doc_id}.pdf"
    file_origin = str(pdf_path.resolve()) if pdf_path.exists() else f"{safe_doc_id}.pdf"

    try:
        state_writer.append(asiento_folder, {
            "status": "uploaded",
            "reason": "prescan_override_as_single",
            "actor": "user",
            "file_origin": file_origin,
        })
    except Exception as exc:
        logger.error("[override-prescan] append falló: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"No se pudo overridear: {exc}")

    logger.info(
        "[override-prescan] doc_id=%s libro=%s forced as_single",
        safe_doc_id, book_id,
    )
    return {"doc_id": safe_doc_id, "status": "uploaded", "forced_as_single": True}


@app.delete("/api/books/{book_id}/files/{filename}")
def delete_book_file(book_id: str, filename: str):
    """Elimina un PDF del inbox de forma definitiva (hard delete).

    Política: el operario espera que pulsar la 'X' borre el archivo por
    completo, sin dejar rastro. Única excepción: el asiento ya está en
    ``confirmed`` (exportado a Intermega) — devolvemos 409 para evitar
    desincronizar el libro contable cerrado.

    Hard delete = ``shutil.rmtree(asiento_folder)`` + ``file_path.unlink()``.
    No conservamos sidecar — la trazabilidad se mantiene en
    ``resultado_final.json`` (para asientos ya confirmados) y en los
    logs estructurados del orquestador.
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}")

    if is_pipeline_locked():
        raise HTTPException(status_code=409, detail="Pipeline is running — cannot delete files")

    safe_name = pathlib.PurePosixPath(filename).name
    if not safe_name or safe_name.startswith(".") or ".." in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = _inbox_for_book(book_id) / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    doc_id = os.path.splitext(safe_name)[0]
    libro_short = BOOK_CONFIGS[book_id]["libro_short"]
    asiento_folder = get_asientos_dir() / f"{libro_short}_{doc_id}"
    asiento_deleted = False

    if asiento_folder.exists():
        try:
            current = state_writer.current_status(asiento_folder)
        except Exception:
            current = None
        if current == "confirmed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{safe_name}' ya está exportada a Intermega (asiento "
                    "contable cerrado). No se puede eliminar."
                ),
            )
        try:
            shutil.rmtree(asiento_folder)
            asiento_deleted = True
            logger.info("[delete] Hard delete folder=%s status_previo=%s",
                        asiento_folder.name, current)
        except OSError as e:
            logger.error("[delete] No se pudo borrar carpeta %s: %s",
                         asiento_folder, e, exc_info=True)

    file_path.unlink()
    _invoices_cache.clear()
    _stats_cache.clear()
    _clients_cache.clear()
    return {
        "deleted": safe_name,
        "asiento_deleted": asiento_deleted,
        "asiento_folder": asiento_folder.name if asiento_deleted else None,
    }


# ──────────────────────────────────────────────────────────
# POST /api/pipeline/confirm — confirmación en lote de asientos
# ──────────────────────────────────────────────────────────


class CampoFinal(BaseModel):
    """Valor de un campo tras la revisión humana."""

    valor: Any = None


class AsientoConfirm(BaseModel):
    campos_finales: Dict[str, CampoFinal]
    lineas_asiento: list[dict[str, Any]] = []
    csv_b64: str = ""
    # Override del libro decidido por el operario en fase 3 de revisión.
    # Si presente y válido ("compras"/"ventas"/"bienes"), prevalece sobre
    # el libro derivado del nombre de carpeta del splitter (`_libro_from_folder`).
    # Si ausente o vacío, se mantiene el comportamiento original.
    libro: Optional[str] = None


class ConfirmBatchPayload(BaseModel):
    doc_ids: list[str]
    asientos: Dict[str, AsientoConfirm]


def _pending_confirm_path() -> Path:
    return get_runtime_dir() / ".pending_confirm.json"


def _delete_pending_confirm() -> None:
    p = _pending_confirm_path()
    try:
        if p.exists():
            p.unlink()
    except OSError as e:
        logger.warning("[confirm] no se pudo borrar pending_confirm: %s", e)


@app.post("/api/pipeline/confirm")
def confirm_batch(payload: ConfirmBatchPayload):
    """
    Confirma en lote un grupo de asientos revisados por el operario.

    Por cada doc_id:
      1. Localiza la carpeta del asiento (state_writer.find_folder_by_doc_id).
      2. final_writer escribe resultado_final.json + asiento_{doc_id}.csv +
         hash. Calcula el diff frente a resultado_validacion.json y marca
         qué campos llevan `editado: true`.
      3. Si state aún no es ``done`` (idempotencia), añade evento done al
         sidecar.
      4. Acumula la actualización del maestro de clientes.

    Al cierre del lote, guarda el maestro UNA sola vez bajo file-lock y borra
    `.pending_confirm.json` si existía.
    """
    if not payload.doc_ids:
        raise HTTPException(status_code=400, detail="doc_ids vacío")

    cfg = settings()
    maestro = cargar_maestro(cfg.maestro_clientes_path)
    nifs_antes = set(maestro.get("clientes", {}).keys())

    # Atomicidad: si un doc falla parcialmente (sidecar corrupto, OSError),
    # NO aborta el batch — registramos el error en ``per_doc_errors`` y
    # continuamos. Al final guardamos el maestro con los docs que SÍ se
    # confirmaron correctamente. Sin esto, un sidecar roto en el doc N
    # tiraba la HTTPException y dejaba el maestro sin actualizar para los
    # 1..N-1 ya escritos en disco — los asientos quedaban en limbo: CSV
    # generado pero historial /api/clients sin reflejo.
    _NIF_KEYS = {"nif_entidad", "nif_receptor", "nif_cliente"}
    confirmadas = 0
    per_doc_errors: list[dict[str, str]] = []
    confirmed_doc_ids: list[str] = []  # docs que pasaron sin error

    for doc_id in payload.doc_ids:
        try:
            validate_doc_id(doc_id)
        except HTTPException as e:
            per_doc_errors.append({"doc_id": doc_id, "error": e.detail, "stage": "validate"})
            continue
        if doc_id not in payload.asientos:
            per_doc_errors.append({
                "doc_id": doc_id, "error": "asientos[...] ausente en payload", "stage": "payload",
            })
            continue

        folder = get_doc_folder(doc_id)
        if folder is None:
            per_doc_errors.append({"doc_id": doc_id, "error": "folder no encontrado", "stage": "lookup"})
            continue

        asiento = payload.asientos[doc_id]
        try:
            csv_bytes = base64.b64decode(asiento.csv_b64, validate=False)
        except (ValueError, TypeError) as e:
            per_doc_errors.append({"doc_id": doc_id, "error": f"csv_b64 inválido: {e}", "stage": "csv"})
            continue

        libro_folder = _libro_from_folder(folder.name) or ""
        # Override del operario (fase 3 de revisión): si la cuenta contable
        # corregida implicaba otro libro, el frontend envía `asiento.libro`
        # como forma corta ("compras"/"ventas"/"bienes"). Validamos contra el
        # set conocido para evitar payloads malformados.
        libro_override_raw = (asiento.libro or "").strip().lower() or None
        if libro_override_raw and libro_override_raw not in {"compras", "ventas", "bienes"}:
            per_doc_errors.append({
                "doc_id": doc_id,
                "error": f"libro override inválido: {libro_override_raw!r}",
                "stage": "validate",
            })
            continue
        libro_short = libro_override_raw or libro_folder
        libro_was_overridden = bool(libro_override_raw) and libro_override_raw != libro_folder
        if libro_was_overridden:
            logger.info(
                "[confirm] override de libro doc_id=%s folder=%s confirmado=%s",
                doc_id, libro_folder or "?", libro_short,
            )
        # Normalizamos NIFs a mayúsculas antes de persistir. Sin esto el
        # frontend podía guardar "49915950q" minúscula y romper las
        # comparaciones case-sensitive del historial.
        campos_para_writer = {
            k: {"valor": (str(v.valor).strip().upper() if k in _NIF_KEYS and v.valor else v.valor)}
            for k, v in asiento.campos_finales.items()
        }

        try:
            final_writer.write_final(
                str(folder),
                doc_id=doc_id,
                libro=libro_short,
                campos_finales=campos_para_writer,
                lineas=asiento.lineas_asiento,
                csv_bytes=csv_bytes,
            )
        except Exception as e:
            logger.error(
                "[confirm] final_writer falló doc_id=%s: %s",
                doc_id, e, exc_info=True,
            )
            per_doc_errors.append({"doc_id": doc_id, "error": str(e), "stage": "final_writer"})
            continue

        # Idempotencia específica del batch confirm. Permite registrar el
        # evento aunque exista un `done, action=approve` previo (de la fase
        # de revisión por factura) — son semánticamente distintos y la
        # trazabilidad fiscal exige verlos ambos. Solo se omite si ya hubo
        # un `confirmed` (retry del frontend).
        if not state_writer.is_confirmed(folder):
            campos_finales_event = {
                k: (str(v.valor).strip().upper() if k in _NIF_KEYS and v.valor else v.valor)
                for k, v in asiento.campos_finales.items()
            }
            lineas_event = [dict(l) for l in asiento.lineas_asiento]
            try:
                event_payload: dict[str, Any] = {
                    "status": "confirmed",
                    "actor": "operario",
                    # ``action="confirmed"`` se mantiene para parsers legacy.
                    "action": "confirmed",
                    "libro": libro_short,
                    "campos_finales": campos_finales_event,
                    "lineas_asiento": lineas_event,
                }
                if libro_was_overridden:
                    # Trazabilidad: la carpeta sigue siendo `{libro_folder}_{doc_id}`
                    # pero el libro semántico final difiere. Esto permite que
                    # /historial y auditoría distingan correcciones humanas.
                    event_payload["libro_override"] = True
                    event_payload["libro_original"] = libro_folder
                state_writer.append(folder, event_payload)
            except (FileNotFoundError, ValueError, OSError) as e:
                logger.error(
                    "[confirm] sidecar.append confirmed falló doc_id=%s: %s — el "
                    "CSV ya está en disco pero el sidecar queda sin marcar "
                    "confirmed. Se contabiliza como error parcial.",
                    doc_id, e, exc_info=True,
                )
                per_doc_errors.append({"doc_id": doc_id, "error": str(e), "stage": "sidecar_append"})
                continue

        # Maestro de contactos: actualizar con TODAS las entidades del asiento.
        #
        # La trazabilidad muestra todos los contactos del libro (clientes Y
        # proveedores). Por eso registramos AMBOS NIFs presentes:
        #
        #   - nif_entidad   = emisor de la factura
        #       * compras → proveedor
        #       * ventas  → cliente de la gestoría
        #   - nif_receptor  = receptor de la factura (alias nif_cliente cuando
        #       el ensamblador lo resolvió)
        #       * compras → cliente de la gestoría
        #       * ventas  → cliente del cliente de la gestoría (sin uso oper.)
        #
        # Registrar emisor también garantiza que, aunque el operario apruebe
        # un asiento bloqueado (receptor sin resolver), su trazabilidad por
        # proveedor sigue apareciendo en /historial.
        fecha_exp = (asiento.campos_finales.get("fecha_expedicion") or CampoFinal()).valor

        nifs_ya_registrados: set[str] = set()

        # Derivar roles: emisor y receptor según el libro contable.
        # compras/bienes: emisor = proveedor, receptor = cliente de la gestoría
        # ventas:         emisor = cliente de la gestoría, receptor = su cliente
        if libro_short in ("compras", "bienes"):
            tipo_emisor, tipo_receptor = "proveedor", "cliente"
        else:  # ventas
            tipo_emisor, tipo_receptor = "cliente", "proveedor"

        def _reg(nif_key: str, nombre_key: str, tipo: str) -> None:
            nif_v = (asiento.campos_finales.get(nif_key) or CampoFinal()).valor
            nombre_v = (asiento.campos_finales.get(nombre_key) or CampoFinal()).valor or ""
            if nif_v and libro_short:
                nif_clean = str(nif_v).strip()
                if nif_clean in nifs_ya_registrados:
                    return
                nifs_ya_registrados.add(nif_clean)
                registrar_cliente(
                    maestro,
                    nif_clean,
                    str(nombre_v).strip(),
                    fecha_expedicion=fecha_exp,
                    libro=libro_short,
                    # La confirmación explícita del operario sobreescribe la
                    # incertidumbre del pipeline: registrar siempre.
                    decision="auto",
                    tipo=tipo,
                )

        try:
            # Emisor (siempre presente — campo crítico RD 1619/2012)
            _reg("nif_entidad", "nombre_entidad", tipo_emisor)
            # Receptor — alias frontend lo manda como nif_cliente; backend también
            # acepta nif_receptor por compatibilidad. Probar ambos.
            _reg("nif_cliente", "nombre_cliente", tipo_receptor)
            _reg("nif_receptor", "nombre_receptor", tipo_receptor)
        except Exception as e:
            # Un fallo registrando en maestro NO debe revertir el sidecar.confirmed
            # (asiento contable ya cerrado); solo dejamos rastro y seguimos.
            logger.error(
                "[confirm] error actualizando maestro doc_id=%s: %s",
                doc_id, e, exc_info=True,
            )
            per_doc_errors.append({
                "doc_id": doc_id, "error": f"maestro_update: {e}", "stage": "maestro_partial",
            })
            # Aun así contamos como confirmada (sidecar.confirmed sí pasó).

        confirmadas += 1
        confirmed_doc_ids.append(doc_id)

    # Una sola escritura del maestro, bajo lock cross-process.
    guardar_maestro(cfg.maestro_clientes_path, maestro)

    nifs_despues = set(
        cargar_maestro(cfg.maestro_clientes_path).get("clientes", {}).keys()
    )
    clientes_nuevos = len(nifs_despues - nifs_antes)

    # Limpieza post-confirm: borrar los PDFs del inbox de las facturas que
    # acabamos de confirmar. El asiento contable ya está en resultado_final.json
    # + CSV + audit JSONL; el PDF del inbox no aporta nada operativo y solo
    # ensucia el flujo "CONFIRMAR Y SEGUIR ESCANEANDO".
    #
    # Solo borramos PDFs de docs que pasaron sin error (``confirmed_doc_ids``).
    # Si hubo error parcial, dejamos el PDF en su sitio para que el operario
    # pueda reintentar.
    #
    # Usamos ``file_origin`` del evento ``uploaded`` del sidecar (no
    # ``get_doc_folder`` + reconstrucción) — el sidecar es la fuente canónica
    # y resuelve el caso multi-libro: si el doc_id existe en varios libros,
    # cada uno tiene su propio ``file_origin``.
    inbox_pdfs_deleted = 0
    for doc_id in confirmed_doc_ids:
        folder = get_doc_folder(doc_id)
        if folder is None:
            continue
        try:
            state_data = state_writer.read(folder)
        except Exception:
            continue
        # Encontrar el primer evento ``uploaded`` con file_origin (es el path
        # absoluto al PDF del inbox, anotado por /api/upload_files).
        file_origin = None
        for ev in state_data.get("events") or []:
            if ev.get("status") == "uploaded" and ev.get("file_origin"):
                file_origin = ev["file_origin"]
                break
        # Fallback: sidecars legacy sin evento ``uploaded``. Reconstruimos
        # la ruta usando el libro derivado del folder_name y el doc_id como
        # basename (operación idempotente — si no existe, no pasa nada).
        if not file_origin:
            libro_short_local = _libro_from_folder(folder.name)
            if libro_short_local:
                guessed = (
                    Path(settings().inbox_path(libro_short_local))
                    / f"{doc_id}.pdf"
                )
                if guessed.is_file():
                    file_origin = str(guessed)
        if not file_origin:
            continue
        try:
            pdf_path = Path(file_origin)
            if pdf_path.is_file():
                pdf_path.unlink()
                inbox_pdfs_deleted += 1
                logger.info(
                    "[confirm] PDF inbox eliminado tras confirm doc_id=%s path=%s",
                    doc_id, pdf_path,
                )
        except OSError as e:
            logger.warning(
                "[confirm] No se pudo borrar PDF inbox doc_id=%s: %s",
                doc_id, e,
            )

    _invoices_cache.clear()
    _stats_cache.clear()
    _clients_cache.clear()
    _delete_pending_confirm()

    logger.info(
        "[confirm] lote confirmado: %d facturas, %d clientes nuevos, %d PDFs inbox eliminados, %d errores parciales",
        confirmadas, clientes_nuevos, inbox_pdfs_deleted, len(per_doc_errors),
    )
    return {
        # ``ok`` solo si no hubo errores parciales. El frontend puede mostrar
        # un toast verde si ok=true; amarillo si ok=false con detalle.
        "ok": len(per_doc_errors) == 0,
        "facturas_confirmadas": confirmadas,
        "clientes_nuevos": clientes_nuevos,
        "inbox_pdfs_deleted": inbox_pdfs_deleted,
        "errors": per_doc_errors,
    }


def _load_pending_confirm() -> dict | None:
    p = _pending_confirm_path()
    if not p.exists():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[pending_confirm] no se pudo leer: %s", e)
        return None


_SPLIT_NAME_RE = re.compile(r"^(.+)__(\d+)of(\d+)$", re.IGNORECASE)


def _derive_split_meta(filename_stem: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Si ``filename_stem`` cumple el patrón ``X__NofM``, devuelve (X.pdf, N, M)."""
    m = _SPLIT_NAME_RE.match(filename_stem)
    if not m:
        return None, None, None
    stem, idx_s, total_s = m.group(1), m.group(2), m.group(3)
    try:
        return f"{stem}.pdf", int(idx_s), int(total_s)
    except ValueError:
        return None, None, None


@app.get("/api/pipeline/batch")
def pipeline_batch():
    """Composición del lote del run actual con estado por factura, agrupado por libro.

    Fuente del lote: ``.pending_confirm.json`` (escrito por ``pipeline.run_pipeline``
    al inicio, después del splitter, contiene los doc_ids reales que se van a procesar).
    Para cada doc_id se localiza su carpeta de asiento, se lee el status del
    ``.state.json`` y se deriva metadata de split (si su nombre cumple ``X__NofM``).

    Si no hay lote en curso o el status global está en ``completed``/``idle``,
    devuelve ``in_flight=false`` con la lista vacía — la UI cae al modo legacy.
    """
    pending = _load_pending_confirm()
    doc_ids = list((pending or {}).get("doc_ids_lote") or [])

    # Status global para current_file y para saber si seguimos "in flight".
    status_data: dict = {}
    status_path = _status_path()
    if status_path.exists():
        try:
            with status_path.open(encoding="utf-8") as f:
                status_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            status_data = {}
    global_status = status_data.get("status")
    current_file = status_data.get("current_file")

    in_flight = bool(doc_ids) and global_status not in {"completed", "idle", None}
    if not in_flight:
        return {
            "in_flight": False,
            "current_file": current_file,
            "current_libro": None,
            "books": [],
        }

    asientos_root = get_asientos_dir()
    grouped: dict[str, list[dict]] = {bid: [] for bid in BOOK_CONFIGS}
    current_libro: Optional[str] = None

    for doc_id in doc_ids:
        folder = state_writer.find_folder_by_doc_id(asientos_root, doc_id)
        st = state_writer.current_status(folder) if folder is not None else None
        # Derivar libro_short y filename
        libro_short: Optional[str] = None
        filename = f"{doc_id}.pdf"
        decision: Optional[str] = None
        folder_name: Optional[str] = None
        if folder is not None:
            folder_name = folder.name
            libro_short = _libro_from_folder(folder_name)
            sidecar_data = get_state(folder) or {}
            events = sidecar_data.get("events") or []
            for ev in events:
                origin = ev.get("file_origin")
                if origin:
                    filename = os.path.basename(origin)
                    break
            # `decision` aparece como campo opcional en eventos cierre del pipeline.
            for ev in reversed(events):
                if ev.get("decision"):
                    decision = ev.get("decision")
                    break

        if libro_short is None:
            # Fallback: buscar el filename en los 3 inboxes (caso degenerado
            # sin sidecar aún — no debería pasar tras Bloque 1 pero defendemos).
            for bid in BOOK_CONFIGS:
                inbox = _inbox_for_book(bid)
                if (inbox / filename).exists():
                    libro_short = BOOK_CONFIGS[bid]["libro_short"]
                    break

        # Mapear status del sidecar a `BatchFileStatus`.
        if current_file and filename == current_file:
            ui_status = "processing"
        elif st in {None, "uploaded"}:
            ui_status = "pending"
        elif st in {"done", "review", "confirmed", "blocked", "error", "processing", "retrying"}:
            # `processing` del sidecar significa "se está procesando"; si no
            # coincide con current_file es un sidecar dejado a medias por un
            # run anterior — tratamos como `pending` operativo para no engañar.
            ui_status = "processing" if st in {"processing", "retrying"} and current_file is None else (
                st if st in {"done", "review", "confirmed", "blocked", "error"} else "pending"
            )
        elif st in {"split", "cancelled", "renamed", "reset"}:
            # Estos no deberían aparecer en el lote (los splits originales son
            # archivados, los cancelados/reset no entran). Si aparecen, pending.
            ui_status = "pending"
        else:
            ui_status = "pending"

        split_origin, split_index, split_total = _derive_split_meta(doc_id)

        entry = {
            "doc_id": doc_id,
            "filename": filename,
            "status": ui_status,
            "folder_name": folder_name,
            "decision": decision,
            "split_origin": split_origin,
            "split_index": split_index,
            "split_total": split_total,
        }

        if ui_status == "processing":
            current_libro = libro_short

        target_book_id = _LIBRO_SHORT_TO_BOOK.get(libro_short) if libro_short else None
        if target_book_id is None:
            # Sin libro identificable: lo metemos en la primera caja para que
            # el operario lo vea. Mejor visible que perdido.
            target_book_id = next(iter(BOOK_CONFIGS))
        grouped[target_book_id].append(entry)

    books_out = []
    for bid, cfg_book in BOOK_CONFIGS.items():
        entries = grouped.get(bid) or []
        if not entries:
            continue
        books_out.append({
            "book_id": bid,
            "libro": cfg_book["libro_short"],
            "label": cfg_book["label"],
            "files": entries,
        })

    return {
        "in_flight": True,
        "current_file": current_file,
        "current_libro": current_libro,
        "books": books_out,
    }


@app.get("/api/pipeline/status")
def pipeline_status():
    """Lee el estado del pipeline (runtime en `libros/.runtime/`)."""
    pending = _load_pending_confirm()
    pending_flag = {
        "pending_confirm": pending is not None,
        "pending_doc_ids": (pending or {}).get("doc_ids_lote", []),
    }

    status_path = _status_path()
    if not status_path.exists():
        return {
            "status": "idle",
            "processed": 0,
            "total": 0,
            "current_file": None,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            **pending_flag,
        }
    try:
        with status_path.open(encoding="utf-8") as f:
            data = json.load(f)
        data.update(pending_flag)
        return data
    except Exception as e:
        logger.error(f"Error reading pipeline status: {e}", exc_info=True)
        return {"status": "error", "error_message": str(e), **pending_flag}


@app.post("/api/pipeline/run")
async def run_pipeline_endpoint():
    """Lanza el pipeline en background sobre los inboxes permanentes.

    Trazabilidad 2.0:
    - Sin copia previa a un sandbox: ``run_pipeline`` escanea directamente
      ``libros/facturas/{libro}/``.
    - Sin `file_manifest.json`: el libro se infiere del path del inbox.
    - Sin borrado de archivos al terminar: los PDFs viven permanentemente
      en su inbox; el estado de procesado vive en el `.state.json` del
      asiento correspondiente.
    """
    if not acquire_pipeline_lock():
        raise HTTPException(status_code=409, detail="Pipeline already running")

    cfg = settings()

    # Índice doc_id → status para excluir facturas ya confirmadas (done).
    # Coherente con /api/books, que ya las oculta del inbox visible.
    done_doc_ids: set[str] = set()
    for folder in _scan_asiento_folders():
        st = get_state(folder) or {}
        doc_id = st.get("doc_id")
        events = st.get("events") or []
        if doc_id and events and events[-1].get("status") == "done":
            done_doc_ids.add(doc_id)

    # Mapa book_id → (inbox, libro_long, libro_short). Solo libros con
    # archivos pendientes (los "done" se omiten igual que en la UI).
    book_inbox_map: dict[str, dict] = {}
    total_files = 0
    for book_id, cfg_book in BOOK_CONFIGS.items():
        inbox = _inbox_for_book(book_id)
        if not inbox.is_dir():
            continue
        files = [
            p for p in inbox.iterdir()
            if p.is_file() and os.path.splitext(p.name)[0] not in done_doc_ids
        ]
        if files:
            book_inbox_map[book_id] = {
                "inbox": inbox,
                "libro_long": cfg_book["libro_long"],
                "libro_short": cfg_book["libro_short"],
                "count": len(files),
            }
            total_files += len(files)

    if total_files == 0:
        release_pipeline_lock()
        return {"status": "started", "total": 0}

    # Escribir estado inicial en `libros/.runtime/pipeline_status.json`.
    status_path = _status_path()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    initial_status = {
        "status": "running",
        "processed": 0,
        "total": total_files,
        "current_file": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error_message": None,
    }
    try:
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(initial_status, f, indent=2)
    except Exception as e:
        logger.error(f"Error writing pipeline status: {e}", exc_info=True)

    def _run_pipeline_sync():
        try:
            # Inicializar logging del pipeline (JSONL + console).
            from src.logging_config import setup_logging
            setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

            from src.pipeline import run_pipeline

            processed_acc = 0
            for book_id, info in book_inbox_map.items():
                if is_cancel_requested():
                    logger.warning("[pipeline] Cancelación solicitada — abortando libros pendientes")
                    break

                logger.info(
                    "[pipeline] Procesando libro=%s (%d archivos) desde %s",
                    info["libro_long"], info["count"], info["inbox"],
                )
                book_summary = run_pipeline(
                    str(info["inbox"]),
                    info["libro_long"],
                    status_file=str(status_path),
                    processed_offset=processed_acc,
                    cancel_requested=is_cancel_requested,
                    is_multi_book=True,
                )
                processed_acc += (
                    book_summary["ok"] + book_summary["warn"] + book_summary["error"]
                )

            # Estado final.
            try:
                with status_path.open("r", encoding="utf-8") as f:
                    status = json.load(f)
                if is_cancel_requested():
                    status["status"] = "cancelled"
                    status["error_message"] = "Procesamiento cancelado por el usuario"
                else:
                    status["status"] = "completed"
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                with status_path.open("w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Pipeline background: error escribiendo status final: %s", exc, exc_info=True)
        except Exception as e:
            logger.error(f"Pipeline background task failed: {e}", exc_info=True)
            try:
                with status_path.open("r", encoding="utf-8") as f:
                    status = json.load(f)
                status["status"] = "error"
                status["error_message"] = str(e)
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                with status_path.open("w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Pipeline background: error escribiendo status de error: %s", exc, exc_info=True)
        finally:
            release_pipeline_lock()
            # Invalidar cachés tras un run para que /api/invoices y /api/stats
            # reflejen los nuevos asientos sin esperar al TTL.
            _invoices_cache.clear()
            _stats_cache.clear()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_pipeline_sync)

    return {"status": "started", "total": total_files}


# ──────────────────────────────────────────────────────────
# Reset endpoint — limpia carpetas de asiento y runtime
# ──────────────────────────────────────────────────────────

@app.post("/api/pipeline/reset")
def reset_pipeline(wipe_inbox: bool = True):
    """
    Reset operativo del pipeline (trazabilidad 2.0):

    1. Borra las carpetas de asiento NO confirmadas (`libros/asientos/*` con
       último status del sidecar != ``confirmed``) — incluye sus `.state.json`
       y artefactos JSON. Las confirmadas se preservan: son historial fiscal
       visible en /asientos y eliminarlas sería pérdida de datos contables.
    2. Limpia el estado transitorio (`libros/.runtime/*`).
    3. Si ``wipe_inbox=True`` (default), borra también los PDFs del inbox
       (`libros/facturas/*/*.pdf`). El flujo "Nuevo escaneo" pasa por aquí
       para dejar un estado limpio. Si ``wipe_inbox=False``, los PDFs se
       conservan para reproceso (útil en operaciones de recuperación).
    4. Registra el evento en `libros/logs/audit/reset_{fecha}.jsonl` para
       trazabilidad fiscal.
    """
    if is_pipeline_locked():
        raise HTTPException(status_code=409, detail="Pipeline en ejecución. No se puede resetear.")

    cfg = settings()
    asientos_root = get_asientos_dir()
    runtime_root = get_runtime_dir()

    # 0. Volcar sidecars `.state.json` a un dump audit antes de borrar nada,
    #    para que la trazabilidad post-reset sea reconstruible si hace falta
    #    forense (¿qué se borró? ¿en qué estado estaba?).
    ts_reset = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_dir = Path(cfg.audit_path())
    dump_dir = audit_dir / "state_dumps" / ts_reset
    sidecars_dumped = 0
    # Solo volcamos sidecars de los que SÍ vamos a borrar (no-confirmados).
    # Los confirmados se preservan en disco, no necesitan dump forense.
    try:
        if asientos_root.exists():
            dump_dir.mkdir(parents=True, exist_ok=True)
            for entry in asientos_root.iterdir():
                if not entry.is_dir():
                    continue
                sidecar = entry / state_writer.SIDECAR_NAME
                if not sidecar.exists():
                    continue
                if state_writer.is_confirmed(entry):
                    continue
                try:
                    target = dump_dir / f"{entry.name}.json"
                    shutil.copy2(str(sidecar), str(target))
                    sidecars_dumped += 1
                except OSError as e:
                    logger.warning("[reset] No se pudo volcar sidecar %s: %s", entry.name, e)
    except Exception as e:
        logger.error("[reset] Error preparando dump de sidecars: %s", e, exc_info=True)

    # 1. Borrar carpetas de asiento NO confirmadas (incluye .state.json +
    #    artefactos JSON). Las confirmadas son historial fiscal: se preservan.
    asientos_deleted = 0
    asientos_preserved = 0
    if asientos_root.exists():
        for entry in asientos_root.iterdir():
            if not entry.is_dir():
                continue
            if state_writer.is_confirmed(entry):
                asientos_preserved += 1
                continue
            try:
                shutil.rmtree(entry)
                asientos_deleted += 1
            except OSError as e:
                logger.error("[reset] Error borrando %s: %s", entry, e, exc_info=True)

    # 2. Limpiar runtime (lock, cancel, status).
    if runtime_root.exists():
        for entry in runtime_root.iterdir():
            try:
                entry.unlink()
            except (OSError, IsADirectoryError):
                pass

    # 2b. Si wipe_inbox=True, limpiar también los PDFs del inbox.
    #     Necesario para que el flujo "Nuevo escaneo" deje un estado limpio:
    #     tras un upload con colisiones renombradas + reset, los PDFs huérfanos
    #     quedaban en el inbox y polucionaban el GET /api/books.
    inbox_files_deleted = 0
    if wipe_inbox:
        cfg2 = settings()
        for book_cfg in BOOK_CONFIGS.values():
            inbox_dir = Path(cfg2.inbox_path(book_cfg["libro_short"]))
            if not inbox_dir.exists():
                continue
            for entry in inbox_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in ALLOWED_INVOICE_EXTENSIONS:
                    continue
                try:
                    entry.unlink()
                    inbox_files_deleted += 1
                except OSError as e:
                    logger.warning("[reset] No se pudo borrar %s: %s", entry, e)

    # 3. Registrar evento en audit log para trazabilidad fiscal.
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        reset_record = {
            "schema_v": 1,
            "ts_proceso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "reset_batch",
            "asientos_deleted": asientos_deleted,
            "asientos_preserved": asientos_preserved,
            "sidecars_dumped": sidecars_dumped,
            "inbox_files_deleted": inbox_files_deleted,
            "wipe_inbox": wipe_inbox,
            "dump_dir": str(dump_dir) if sidecars_dumped else None,
        }
        reset_log = audit_dir / f"reset_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        with reset_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reset_record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("[reset] Error escribiendo audit log: %s", e, exc_info=True)

    # Invalidar cachés de listado/stats.
    _invoices_cache.clear()
    _stats_cache.clear()

    logger.info(
        "[reset] Completado: %d eliminadas, %d preservadas (confirmed), %d sidecars volcados, %d PDFs inbox borrados (wipe=%s)",
        asientos_deleted, asientos_preserved, sidecars_dumped, inbox_files_deleted, wipe_inbox,
    )

    return {
        "status": "ok",
        "asientos_deleted": asientos_deleted,
        "asientos_preserved": asientos_preserved,
        "sidecars_dumped": sidecars_dumped,
        "inbox_files_deleted": inbox_files_deleted,
        "wipe_inbox": wipe_inbox,
        "dump_dir": str(dump_dir) if sidecars_dumped else None,
    }


# ──────────────────────────────────────────────────────────
# GET /api/clients — trazabilidad por cliente (lee maestro_clientes.yaml)
# ──────────────────────────────────────────────────────────

_clients_cache: dict = {}


@app.get("/api/clients")
def list_clients():
    """Lista de clientes registrados con métricas operativas para el grid de
    trazabilidad. Lee `data/maestros/maestro_clientes.yaml` (fuente única).

    Si el YAML no existe → 200 con lista vacía (no es error: gestoría recién
    instalada sin facturas confirmadas).

    Caché en memoria 5s para evitar I/O por cada poll del frontend.
    """
    now = time.time()
    cached = _clients_cache.get("data")
    if cached and now - _clients_cache.get("ts", 0) < _CACHE_TTL:
        return cached

    cfg = settings()
    try:
        maestro = cargar_maestro(cfg.maestro_clientes_path)
    except Exception as e:
        logger.error("[clients] error leyendo maestro: %s", e, exc_info=True)
        return {"clients": [], "total": 0}

    # Fuente de verdad = libros/asientos/. Recolectamos por NIF normalizado:
    # count de asientos, última fecha de expedición y libros activos.
    # ESTO no se lee del maestro porque el maestro tiene contadores stale
    # (resetPipeline borra asientos pero no decrementa documentos_procesados,
    # y el NIF puede haberse guardado con distinta capitalización).
    asientos_por_nif: dict[str, dict] = {}
    for folder in _scan_asiento_folders():
        final_path = folder / "resultado_final.json"
        if not final_path.exists():
            continue
        try:
            final = load_json_file(final_path)
        except HTTPException:
            continue
        campos = final.get("campos_finales") or {}
        nif_cliente = (campos.get("nif_cliente") or {}).get("valor")
        if not nif_cliente:
            continue
        nif_norm = str(nif_cliente).strip().upper()
        fecha = (campos.get("fecha_expedicion") or {}).get("valor")
        libro = final.get("libro")
        # nombre_cliente es el campo canónico del frontend, pero si llegó
        # null (cliente.nombre era falsy en el confirm) caemos al campo que
        # juega el rol de cliente de la gestoría según el libro:
        #   ventas         → nombre_entidad (emisor = cliente de la gestoría)
        #   compras/bienes → nombre_receptor (receptor = cliente de la gestoría)
        nombre_doc = str(
            (campos.get("nombre_cliente") or {}).get("valor") or ""
        ).strip()
        if not nombre_doc:
            fallback_key = "nombre_entidad" if libro == "ventas" else "nombre_receptor"
            nombre_doc = str(
                (campos.get(fallback_key) or {}).get("valor") or ""
            ).strip()
        entry = asientos_por_nif.setdefault(
            nif_norm,
            {
                "count": 0,
                "ultima_factura_fecha": None,
                "libros": set(),
                # nombre = el de la factura confirmada más reciente con
                # nombre_cliente no vacío. Permite que el historial muestre el
                # nombre correcto aunque el maestro tenga la entrada con
                # nombre="" por un OCR fallido en la primera confirmación.
                "nombre": "",
                "_nombre_fecha": None,
            },
        )
        entry["count"] += 1
        if fecha and (entry["ultima_factura_fecha"] is None or fecha > entry["ultima_factura_fecha"]):
            entry["ultima_factura_fecha"] = fecha
        if libro:
            entry["libros"].add(libro)
        if nombre_doc:
            # Más reciente gana; si la nueva no tiene fecha pero la entry está
            # vacía, también rellena (mejor algo que nada).
            current_fecha = entry["_nombre_fecha"]
            if (
                not entry["nombre"]
                or (fecha and (current_fecha is None or fecha >= current_fecha))
            ):
                entry["nombre"] = nombre_doc
                entry["_nombre_fecha"] = fecha

    # Dedupe entradas del maestro por NIF normalizado. El maestro aporta
    # metadatos no-derivables (nombre, fecha_alta, tipos_activos).
    clientes_dict = maestro.get("clientes", {}) or {}
    meta_by_upper: dict[str, dict] = {}
    for nif, info in clientes_dict.items():
        upper = (nif or "").strip().upper()
        if not upper:
            continue
        tipos = info.get("tipos_activos", []) or []
        if "cliente" not in tipos:
            continue
        # Si hay duplicado por case, ganamos la entrada con más documentos
        # (proxy de "la que más se ha usado").
        existing = meta_by_upper.get(upper)
        if existing is None or info.get("documentos_procesados", 0) > existing.get("documentos_procesados", 0):
            meta_by_upper[upper] = info

    # Cruce: cliente aparece sii (a) está en maestro como "cliente" y
    # (b) tiene al menos un asiento confirmado. Todas las stats salen de los
    # asientos reales — el maestro solo aporta nombre/fecha_alta.
    items = []
    for upper_nif, agg in asientos_por_nif.items():
        info = meta_by_upper.get(upper_nif)
        if info is None:
            continue
        # Verdad fiscal del documento prima sobre el maestro: si algún
        # resultado_final.json del NIF trae nombre_cliente no vacío, ese gana.
        # Cae al maestro solo cuando todos los documentos del NIF están sin
        # nombre (caso patológico que el frontend no debería producir).
        nombre = agg.get("nombre") or info.get("nombre", "")
        items.append({
            "nif": upper_nif,
            "nombre": nombre,
            "fecha_alta": info.get("fecha_alta"),
            "ultima_factura_fecha": agg["ultima_factura_fecha"],
            "documentos_procesados": agg["count"],
            "libros_activos": sorted(agg["libros"]),
            "tipos_activos": info.get("tipos_activos", []) or [],
        })

    # Orden desc por ultima_factura_fecha (None al final).
    items.sort(
        key=lambda c: (c["ultima_factura_fecha"] is None, c["ultima_factura_fecha"] or ""),
        reverse=False,
    )
    items.sort(key=lambda c: c["ultima_factura_fecha"] or "", reverse=True)

    result = {"clients": items, "total": len(items)}
    _clients_cache["data"] = result
    _clients_cache["ts"] = now
    return result


# TODO: con >2000 facturas, construir índice nif→[doc_ids] al startup en
# lugar de hacer un escaneo lineal por petición.
@app.get("/api/clients/{nif}/invoices")
def list_invoices_by_client(nif: str):
    """Facturas confirmadas para un NIF (filtradas por resultado_final.json).

    Excluye facturas sin resultado_final.json (no confirmadas todavía) y las
    bloqueadas o con error técnico en resultado_validacion.json.
    """
    if not nif or "/" in nif or "\\" in nif or ".." in nif:
        raise HTTPException(status_code=400, detail="NIF inválido")

    # Comparación case-insensitive: el frontend puede haber guardado el NIF
    # en minúsculas ("49915950q") aunque la URL llegue normalizada ("49915950Q").
    nif_norm = nif.strip().upper()

    invoices = []
    for folder in _scan_asiento_folders():
        final_path = folder / "resultado_final.json"
        if not final_path.exists():
            continue
        try:
            final = load_json_file(final_path)
        except HTTPException:
            continue

        campos = final.get("campos_finales") or {}
        # Una factura pertenece a un cliente de la gestoría únicamente si su
        # NIF coincide con `nif_cliente` (el rol resuelto por phase4 según
        # `LIBRO_A_ROL_CLIENTE`). No basta con que aparezca como contraparte
        # (emisor/receptor del otro lado), eso lo duplicaría en el panel del
        # otro cliente.
        nif_cliente_raw = (campos.get("nif_cliente") or {}).get("valor")
        if not nif_cliente_raw:
            continue
        if str(nif_cliente_raw).strip().upper() != nif_norm:
            continue

        # `resultado_final.json` solo existe tras confirm humano. No filtramos
        # por `origen_decision`: si la decisión original del pipeline fue
        # `block` pero el operario aprobó la factura, debe aparecer en su
        # historial. La carpeta sigue siendo trazable a través de `.state.json`.
        origen = final.get("origen_decision")

        tiene_ediciones = any(
            c.get("editado") for c in campos.values() if isinstance(c, dict)
        )

        # Contraparte: el otro extremo del asiento. En `compras`/`bienes` el
        # cliente de la gestoría es `nif_receptor`, así que la contraparte es
        # `nif_entidad` (proveedor). En `ventas` se invierte.
        libro_doc = final.get("libro")
        if libro_doc == "ventas":
            cp_nif = (campos.get("nif_receptor") or {}).get("valor")
            cp_nombre = (campos.get("nombre_receptor") or {}).get("valor")
        else:
            cp_nif = (campos.get("nif_entidad") or {}).get("valor")
            cp_nombre = (campos.get("nombre_entidad") or {}).get("valor")

        invoices.append({
            "doc_id": final.get("doc_id"),
            "numero_factura": (campos.get("numero_factura") or {}).get("valor"),
            "fecha_expedicion": (campos.get("fecha_expedicion") or {}).get("valor"),
            "fecha_operacion": (campos.get("fecha_operacion") or {}).get("valor"),
            "total_euros": safe_float((campos.get("total_euros") or {}).get("valor")),
            "decision_global": origen,
            "status": current_status_of(folder),
            "libro": libro_doc,
            "tiene_ediciones": tiene_ediciones,
            "lineas_asiento": final.get("lineas_asiento", []) or [],
            "contraparte_nif": cp_nif,
            "contraparte_nombre": cp_nombre,
            "concepto": (campos.get("concepto") or {}).get("valor"),
            "cuenta_contable": (campos.get("cuenta_contable") or {}).get("valor"),
            "nif_entidad": (campos.get("nif_entidad") or {}).get("valor"),
            "nombre_entidad": (campos.get("nombre_entidad") or {}).get("valor"),
            "nif_receptor": (campos.get("nif_receptor") or {}).get("valor"),
            "nombre_receptor": (campos.get("nombre_receptor") or {}).get("valor"),
            "campos_editados": [k for k, c in campos.items() if isinstance(c, dict) and c.get("editado")],
        })

    invoices.sort(key=lambda i: i.get("fecha_operacion") or i.get("fecha_expedicion") or "", reverse=True)
    return {"nif": nif, "invoices": invoices}


# ──────────────────────────────────────────────────────────
# Cancelación de pipeline en curso
# ──────────────────────────────────────────────────────────

@app.post("/api/pipeline/cancel")
def cancel_pipeline():
    """
    Solicita la cancelación del pipeline en curso. La cancelación es cooperativa:
    deja que la factura actual termine y aborta la siguiente.
    """
    if not is_pipeline_locked():
        raise HTTPException(status_code=409, detail="No hay pipeline en ejecución")
    try:
        _cancel_path().parent.mkdir(parents=True, exist_ok=True)
        _cancel_path().write_text(
            json.dumps({"requested_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error("[cancel] No se pudo escribir flag de cancelación: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="No se pudo solicitar la cancelación")
    return {"status": "cancel_requested"}


# ──────────────────────────────────────────────────────────
# Logs recientes (para tab de Diagnóstico en UI)
# ──────────────────────────────────────────────────────────

@app.get("/api/logs/recent")
def get_recent_logs(n: int = 200):
    """
    Devuelve las últimas N líneas del log estructurado del pipeline.
    Cada línea es un objeto JSON. Se acotan parámetros para evitar abusos.
    """
    n = max(1, min(n, 1000))
    cfg = settings()
    log_path = Path(cfg.logs_path) / "pipeline.jsonl"
    if not log_path.exists():
        return {"lines": [], "total": 0}

    try:
        # Leemos las últimas N líneas con deque (memoria O(N))
        with log_path.open("r", encoding="utf-8") as f:
            tail = deque(f, maxlen=n)
        parsed = []
        for raw in tail:
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                parsed.append({"raw": raw})
        return {"lines": parsed, "total": len(parsed)}
    except OSError as exc:
        logger.error("[logs] Error leyendo %s: %s", log_path, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="No se pudieron leer los logs")


# ──────────────────────────────────────────────────────────
# Versionado y comprobación de actualización
# ──────────────────────────────────────────────────────────

_latest_version_cache: dict = {}
_LATEST_VERSION_TTL = 300  # 5 min — balance entre no martillear Cloudflare y permitir iteración razonable


def _is_newer_version(latest: str, current: str) -> bool:
    """
    True si latest > current en sentido semver (X.Y.Z).
    Soporta sufijos comunes (-dev, -rc.N) tratándolos como pre-releases.
    Si current == "0.0.0-dev" (build local sin tag), cualquier latest publicada cuenta como newer.
    Si parsing falla, devuelve latest != current (fallback conservador).
    """
    if not latest or not current:
        return False
    try:
        def parse(v: str) -> tuple[int, int, int, int]:
            # "0.2.5-rc.1" → (0, 2, 5, 0); "0.2.5" → (0, 2, 5, 1)
            base, _, suffix = v.partition("-")
            parts = base.split(".")
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            is_release = 0 if suffix else 1
            return (major, minor, patch, is_release)
        return parse(latest) > parse(current)
    except (ValueError, IndexError):
        return latest != current


@app.get("/api/system/version")
def get_system_version():
    """Versión instalada del sistema (inyectada en build via env)."""
    return {
        "version": APP_VERSION,
        "gestoria_nif": GESTORIA_NIF,
        "gestoria_nombre": GESTORIA_NOMBRE,
    }


@app.get("/api/system/latest-version")
def get_latest_version(force: bool = False):
    """
    Consulta el JSON estático con la versión más reciente publicada.
    Cacheado 5 min para evitar martillear el endpoint. Pasar ?force=true para
    saltarse el cache (útil para botón "Comprobar ahora" + tests).
    """
    now = time.time()
    if not force and _latest_version_cache and (now - _latest_version_cache.get("ts", 0)) < _LATEST_VERSION_TTL:
        cached = _latest_version_cache["data"]
        logger.info(
            "[system] latest-version (cache hit): latest=%s current=%s update_available=%s",
            cached.get("version"), APP_VERSION, cached.get("update_available"),
        )
        return cached

    try:
        req = urllib.request.Request(
            LATEST_VERSION_URL,
            headers={"User-Agent": f"AIWAF/{APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.warning("[system] No se pudo obtener latest-version: %s", exc)
        return {
            "version": None,
            "update_available": False,
            "current": APP_VERSION,
            "error": "No se pudo contactar con el servidor de versiones",
        }

    latest = data.get("version")
    update_available = _is_newer_version(latest, APP_VERSION)
    logger.info(
        "[system] latest-version (fetched): latest=%s current=%s update_available=%s force=%s",
        latest, APP_VERSION, update_available, force,
    )
    payload = {
        "version": latest,
        "update_available": update_available,
        "current": APP_VERSION,
        "changelog": data.get("changelog", ""),
        "released_at": data.get("released_at"),
    }
    _latest_version_cache["data"] = payload
    _latest_version_cache["ts"] = now
    return payload


WATCHTOWER_URL = os.getenv("WATCHTOWER_URL", "http://watchtower:8080")
WATCHTOWER_TOKEN = os.getenv("WATCHTOWER_HTTP_API_TOKEN", "")


@app.post("/api/system/update")
def request_system_update():
    """
    Dispara una actualización on-demand vía Watchtower HTTP API.
    Watchtower hace pull de las imágenes con label
    `com.centurylinklabs.watchtower.enable=true` y recrea los contenedores.
    """
    status_path = _status_path()
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            # pipeline_status.json usa la clave "status" (ver función _write_status del runner).
            if status.get("status") == "running":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "pipeline_busy",
                        "message": "No se puede actualizar mientras se procesan facturas. Espera a que termine el pipeline.",
                    },
                )
        except (json.JSONDecodeError, OSError):
            pass

    if not WATCHTOWER_TOKEN:
        logger.error("[system] WATCHTOWER_HTTP_API_TOKEN no configurado en .env")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "not_configured",
                "message": "Servicio de actualizaciones no configurado (falta WATCHTOWER_HTTP_API_TOKEN). Reinstala el paquete.",
            },
        )

    try:
        resp = httpx.post(
            f"{WATCHTOWER_URL}/v1/update",
            headers={"Authorization": f"Bearer {WATCHTOWER_TOKEN}"},
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        logger.error("[system] Watchtower unreachable at %s: %s", WATCHTOWER_URL, exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "watchtower_unreachable",
                "message": "El servicio de actualizaciones no responde. Reinicia Docker Desktop y reintenta.",
            },
        )
    except httpx.HTTPStatusError as exc:
        body_snippet = (exc.response.text or "")[:500]
        logger.error("[system] Watchtower returned %s: %s", exc.response.status_code, body_snippet)
        if exc.response.status_code in (401, 403):
            code = "auth_failed"
            message = (
                "Token Watchtower rechazado. La instalación está corrupta — contacta soporte."
            )
        else:
            code = "watchtower_error"
            message = f"El servicio de actualizaciones devolvió error {exc.response.status_code}."
        raise HTTPException(
            status_code=502,
            detail={
                "code": code,
                "message": message,
                "watchtower_status": exc.response.status_code,
                "watchtower_body": body_snippet,
            },
        )
    except httpx.HTTPError as exc:
        logger.error("[system] Watchtower request error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "watchtower_error",
                "message": f"Fallo comunicando con el servicio de actualizaciones: {exc.__class__.__name__}.",
            },
        )

    logger.info("[system] Watchtower update triggered OK (status=%s)", resp.status_code)
    return {
        "status": "requested",
        "eta_seconds": 90,
        "message": "Actualización iniciada. Tardará 1-3 minutos.",
    }


# ──────────────────────────────────────────────────────────
# Feedback: "Enviar Problema o Recomendación" → webhook Discord
# ──────────────────────────────────────────────────────────

class FeedbackPayload(BaseModel):
    tipo: str  # "problema" | "recomendacion" | "pregunta"
    descripcion: str
    incluir_logs: bool = False
    navegador: Optional[str] = None


_FEEDBACK_TIPOS = {"problema", "recomendacion", "pregunta"}
_FEEDBACK_COLORS = {
    "problema": 0xE74C3C,       # rojo
    "recomendacion": 0xF1C40F,  # amarillo
    "pregunta": 0x3498DB,       # azul
}
_FEEDBACK_LABELS = {
    "problema": "Problema",
    "recomendacion": "Recomendación",
    "pregunta": "Pregunta",
}


def _persist_feedback(record: dict) -> None:
    """Guarda copia local del feedback en data/feedback/feedback.jsonl."""
    cfg = settings()
    feedback_dir = Path(cfg.output_path).parent / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    target = feedback_dir / "feedback.jsonl"
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _post_to_discord(payload: dict) -> Optional[str]:
    """Envía el feedback como Discord embed. Devuelve mensaje de error si falla."""
    if not FEEDBACK_WEBHOOK_URL:
        return "FEEDBACK_WEBHOOK_URL no configurada"
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            FEEDBACK_WEBHOOK_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"AIWAF/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                return f"Webhook respondió HTTP {resp.status}"
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return str(exc)


@app.post("/api/feedback")
def submit_feedback(payload: FeedbackPayload):
    """
    Recibe feedback del contable y lo reenvía al canal Discord configurado.
    Persiste copia local en data/feedback/feedback.jsonl.
    """
    tipo = payload.tipo.strip().lower()
    if tipo not in _FEEDBACK_TIPOS:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo inválido. Permitidos: {sorted(_FEEDBACK_TIPOS)}",
        )
    descripcion = payload.descripcion.strip()
    if not descripcion or len(descripcion) > 4000:
        raise HTTPException(status_code=400, detail="La descripción debe tener entre 1 y 4000 caracteres")

    timestamp = datetime.now(timezone.utc).isoformat()

    log_excerpt: list[str] = []
    if payload.incluir_logs:
        cfg = settings()
        log_path = Path(cfg.logs_path) / "pipeline.jsonl"
        if log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8") as f:
                    log_excerpt = [line.rstrip() for line in deque(f, maxlen=50)]
            except OSError as exc:
                logger.warning("[feedback] No se pudo leer log: %s", exc)

    record = {
        "tipo": tipo,
        "descripcion": descripcion,
        "navegador": payload.navegador,
        "version": APP_VERSION,
        "gestoria_nif": GESTORIA_NIF,
        "gestoria_nombre": GESTORIA_NOMBRE,
        "timestamp": timestamp,
        "log_excerpt": log_excerpt if payload.incluir_logs else None,
    }

    # Construir embed Discord
    fields = [
        {"name": "Versión", "value": APP_VERSION, "inline": True},
    ]
    if GESTORIA_NOMBRE or GESTORIA_NIF:
        fields.append({
            "name": "Gestoría",
            "value": f"{GESTORIA_NOMBRE} ({GESTORIA_NIF})".strip() or "—",
            "inline": True,
        })
    if payload.navegador:
        fields.append({"name": "Navegador", "value": payload.navegador[:200], "inline": True})

    discord_payload = {
        "embeds": [{
            "title": f"AIWAF · {_FEEDBACK_LABELS[tipo]}",
            "description": descripcion[:1900],
            "color": _FEEDBACK_COLORS[tipo],
            "fields": fields,
            "timestamp": timestamp,
        }]
    }
    if log_excerpt:
        # Discord limita a 2000 chars por content; lo metemos en un segundo embed
        joined = "\n".join(log_excerpt)[-1800:]
        discord_payload["embeds"].append({
            "title": "Últimas 50 líneas de log",
            "description": f"```\n{joined}\n```",
            "color": 0x95A5A6,
        })

    err = _post_to_discord(discord_payload)
    delivered = err is None

    try:
        record["delivered_to_discord"] = delivered
        record["delivery_error"] = err
        _persist_feedback(record)
    except OSError as exc:
        logger.error("[feedback] No se pudo persistir feedback localmente: %s", exc, exc_info=True)

    if not delivered:
        logger.warning("[feedback] No se entregó a Discord: %s", err)

    return {
        "status": "ok",
        "delivered": delivered,
        "delivery_error": err,
    }
