"""
FastAPI application for the invoice processing pipeline.

Provides endpoints for:
- Listing processed invoices
- Getting invoice details (validation results)
- Approving/rejecting invoices from the UI
- Triggering pipeline processing
"""

import asyncio
import json
import logging
import os
import pathlib
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
            for cand in inbox.glob(f"*{doc_id}*"):
                if cand.is_file() and cand.suffix.lower() in ALLOWED_INVOICE_EXTENSIONS:
                    return cand, cand.name
    return None


# ──────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────

@app.get("/api/invoices")
def list_invoices(include_done: bool = True):
    """
    Listar todas las facturas procesadas con su estado.

    Para cada carpeta de asiento devuelve:
    - ``id``        : doc_id estable (basename del PDF original)
    - ``folder_name``: nombre actual de la carpeta (renombrada o provisional)
    - ``libro``     : compras / ventas / bienes (prefijo de la carpeta)
    - ``status``    : último evento del `.state.json` (processing/review/done/...)
    - ``decision_global`` + campos resumen desde `resultado_validacion.json`.

    Query params:
    - ``include_done`` (bool, default True): si False excluye los asientos
      con `status == "done"` (los ya confirmados por el operario). El
      reviewer lo usa para no remostrar facturas viejas tras un ciclo de
      confirmación.
    """
    now = time.time()
    cache_key = f"data_{int(include_done)}"
    if (
        cache_key in _invoices_cache
        and (now - _invoices_cache.get(f"ts_{int(include_done)}", 0)) < _CACHE_TTL
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

        # Filtro: si el operario ya confirmó (`done`), excluir del reviewer.
        if not include_done and status == "done":
            continue

        validation = get_validation_result(folder) or {}
        campos = validation.get("campos", {})

        invoices.append({
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
        })

    result = {"invoices": invoices, "total": len(invoices)}
    _invoices_cache[cache_key] = result
    _invoices_cache[f"ts_{int(include_done)}"] = now
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
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}. Valid: {list(BOOK_CONFIGS.keys())}")

    upload_dir = _inbox_for_book(book_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    errors = []

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

        dest = upload_dir / safe_name
        # Avoid silent overwrite — sufijo timestamp si colisiona.
        if dest.exists():
            base, ext_part = os.path.splitext(safe_name)
            ts = int(datetime.now(timezone.utc).timestamp())
            safe_name = f"{base}_{ts}{ext_part}"
            dest = upload_dir / safe_name

        try:
            with open(dest, "wb") as f:
                content = await file.read()
                f.write(content)
            uploaded.append(safe_name)
        except Exception as e:
            errors.append({"file": file.filename, "error": str(e)})

    return {"uploaded": uploaded, "errors": errors}


@app.delete("/api/books/{book_id}/files/{filename}")
def delete_book_file(book_id: str, filename: str):
    """Elimina un PDF del inbox permanente del libro.

    Solo borra el archivo de entrada — no toca la carpeta de asiento si ya
    existe (esa se gestiona vía `/api/pipeline/reset` o eliminación manual).
    """
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}")

    # Bloquear borrado durante un run activo del pipeline.
    if is_pipeline_locked():
        raise HTTPException(status_code=409, detail="Pipeline is running — cannot delete files")

    safe_name = pathlib.PurePosixPath(filename).name
    if not safe_name or safe_name.startswith(".") or ".." in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = _inbox_for_book(book_id) / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    file_path.unlink()
    return {"deleted": safe_name}


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

    confirmadas = 0
    for doc_id in payload.doc_ids:
        validate_doc_id(doc_id)
        if doc_id not in payload.asientos:
            raise HTTPException(
                status_code=400, detail=f"asientos[{doc_id}] ausente en payload"
            )

        folder = get_doc_folder(doc_id)
        if folder is None:
            raise HTTPException(
                status_code=404, detail=f"doc_id {doc_id} no encontrado"
            )

        asiento = payload.asientos[doc_id]
        try:
            csv_bytes = base64.b64decode(asiento.csv_b64, validate=False)
        except (ValueError, TypeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"csv_b64 inválido para {doc_id}: {e}",
            )

        libro_short = _libro_from_folder(folder.name) or ""
        campos_para_writer = {
            k: {"valor": v.valor} for k, v in asiento.campos_finales.items()
        }

        final_writer.write_final(
            str(folder),
            doc_id=doc_id,
            libro=libro_short,
            campos_finales=campos_para_writer,
            lineas=asiento.lineas_asiento,
            csv_bytes=csv_bytes,
        )

        # Idempotencia: no duplicar evento done si ya está cerrado.
        if not state_writer.is_done(folder):
            try:
                state_writer.append(
                    folder,
                    {"status": "done", "actor": "operario", "action": "confirmed"},
                )
            except (FileNotFoundError, ValueError, OSError) as e:
                logger.error(
                    "[confirm] no se pudo escribir done en sidecar doc_id=%s: %s",
                    doc_id, e, exc_info=True,
                )
                raise HTTPException(status_code=500, detail=str(e))

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
        validation = get_validation_result(folder) or {}
        origen_decision = validation.get("decision_global", "auto")

        def _reg(nif_key: str, nombre_key: str) -> None:
            nif_v = (asiento.campos_finales.get(nif_key) or CampoFinal()).valor
            nombre_v = (asiento.campos_finales.get(nombre_key) or CampoFinal()).valor or ""
            if nif_v and libro_short:
                registrar_cliente(
                    maestro,
                    str(nif_v).strip(),
                    str(nombre_v).strip(),
                    fecha_expedicion=fecha_exp,
                    libro=libro_short,
                    decision=origen_decision,
                )

        # Emisor (siempre presente — campo crítico RD 1619/2012)
        _reg("nif_entidad", "nombre_entidad")
        # Receptor — alias frontend lo manda como nif_cliente; backend también
        # acepta nif_receptor por compatibilidad. Probar ambos.
        _reg("nif_cliente", "nombre_cliente")
        _reg("nif_receptor", "nombre_receptor")

        confirmadas += 1

    # Una sola escritura del maestro, bajo lock cross-process.
    guardar_maestro(cfg.maestro_clientes_path, maestro)

    nifs_despues = set(
        cargar_maestro(cfg.maestro_clientes_path).get("clientes", {}).keys()
    )
    clientes_nuevos = len(nifs_despues - nifs_antes)

    _invoices_cache.clear()
    _stats_cache.clear()
    _clients_cache.clear()
    _delete_pending_confirm()

    logger.info(
        "[confirm] lote confirmado: %d facturas, %d clientes nuevos",
        confirmadas, clientes_nuevos,
    )
    return {
        "ok": True,
        "facturas_confirmadas": confirmadas,
        "clientes_nuevos": clientes_nuevos,
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

    # Mapa book_id → (inbox, libro_long, libro_short). Solo libros con archivos.
    book_inbox_map: dict[str, dict] = {}
    total_files = 0
    for book_id, cfg_book in BOOK_CONFIGS.items():
        inbox = _inbox_for_book(book_id)
        if not inbox.is_dir():
            continue
        files = [p for p in inbox.iterdir() if p.is_file()]
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

            for book_id, info in book_inbox_map.items():
                if is_cancel_requested():
                    logger.warning("[pipeline] Cancelación solicitada — abortando libros pendientes")
                    break

                logger.info(
                    "[pipeline] Procesando libro=%s (%d archivos) desde %s",
                    info["libro_long"], info["count"], info["inbox"],
                )
                run_pipeline(
                    str(info["inbox"]),
                    info["libro_long"],
                    status_file=str(status_path),
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
def reset_pipeline():
    """
    Reset operativo del pipeline (trazabilidad 2.0):

    1. Borra TODAS las carpetas de asiento (`libros/asientos/*`) — incluye
       sus `.state.json` y artefactos JSON.
    2. Limpia el estado transitorio (`libros/.runtime/*`).
    3. Los PDFs originales NO se tocan: siguen en su inbox permanente
       (`libros/facturas/{libro}/`), listos para reproceso.
    4. Registra el evento en `libros/logs/audit/reset_{fecha}.jsonl` para
       trazabilidad fiscal.
    """
    if is_pipeline_locked():
        raise HTTPException(status_code=409, detail="Pipeline en ejecución. No se puede resetear.")

    cfg = settings()
    asientos_root = get_asientos_dir()
    runtime_root = get_runtime_dir()

    # 1. Borrar carpetas de asiento (incluye .state.json + artefactos JSON).
    asientos_deleted = 0
    if asientos_root.exists():
        for entry in asientos_root.iterdir():
            if entry.is_dir():
                try:
                    shutil.rmtree(entry)
                    asientos_deleted += 1
                except OSError as e:
                    logger.error("[reset] Error borrando %s: %s", entry, e, exc_info=True)

    # 2. Limpiar runtime (lock, cancel, status). Los PDFs no se tocan.
    if runtime_root.exists():
        for entry in runtime_root.iterdir():
            try:
                entry.unlink()
            except (OSError, IsADirectoryError):
                pass

    # 3. Registrar evento en audit log para trazabilidad fiscal.
    try:
        audit_dir = Path(cfg.audit_path())
        audit_dir.mkdir(parents=True, exist_ok=True)
        reset_record = {
            "schema_v": 1,
            "ts_proceso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "reset_batch",
            "asientos_deleted": asientos_deleted,
        }
        reset_log = audit_dir / f"reset_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        with reset_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reset_record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("[reset] Error escribiendo audit log: %s", e, exc_info=True)

    # Invalidar cachés de listado/stats.
    _invoices_cache.clear()
    _stats_cache.clear()

    logger.info("[reset] Completado: %d carpetas de asiento eliminadas", asientos_deleted)

    return {
        "status": "ok",
        "asientos_deleted": asientos_deleted,
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

    clientes_dict = maestro.get("clientes", {}) or {}
    items = []
    for nif, info in clientes_dict.items():
        items.append({
            "nif": nif,
            "nombre": info.get("nombre", ""),
            "fecha_alta": info.get("fecha_alta"),
            "ultima_factura_fecha": info.get("ultima_factura_fecha"),
            "documentos_procesados": info.get("documentos_procesados", 0),
            "libros_activos": info.get("libros_activos", []) or [],
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
        # Una factura pertenece a un NIF si ese NIF aparece como emisor,
        # receptor o cliente resuelto. Así el panel de "ÁLVAREZ BRAÑAS"
        # muestra las facturas donde es proveedor (compras) Y donde es
        # cliente (ventas), sin depender de qué resolvió el ensamblador.
        nifs_doc = {
            (campos.get(k) or {}).get("valor")
            for k in ("nif_cliente", "nif_entidad", "nif_receptor")
        }
        nifs_doc = {str(n).strip() for n in nifs_doc if n}
        if nif not in nifs_doc:
            continue

        origen = final.get("origen_decision")
        if origen in {"block", "error"}:
            continue

        tiene_ediciones = any(
            c.get("editado") for c in campos.values() if isinstance(c, dict)
        )

        invoices.append({
            "doc_id": final.get("doc_id"),
            "numero_factura": (campos.get("numero_factura") or {}).get("valor"),
            "fecha_expedicion": (campos.get("fecha_expedicion") or {}).get("valor"),
            "total_euros": safe_float((campos.get("total_euros") or {}).get("valor")),
            "decision_global": origen,
            "status": current_status_of(folder),
            "libro": final.get("libro"),
            "tiene_ediciones": tiene_ediciones,
        })

    invoices.sort(key=lambda i: i["fecha_expedicion"] or "", reverse=True)
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
            timeout=10.0,
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
