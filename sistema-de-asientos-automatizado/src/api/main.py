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

from src.config import settings

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

# Book ID to sandbox folder mapping
BOOK_CONFIGS = {
    "gastos": {
        "label": "Libro de Gastos y Compras",
        "pendientes_subdir": "PENDIENTES/gastos",
        "sandbox_folder": "20_COMPRAS_GASTOS",
    },
    "ingresos": {
        "label": "Libro de Ingresos y Ventas",
        "pendientes_subdir": "PENDIENTES/ingresos",
        "sandbox_folder": "21_VENTAS_INGRESOS",
    },
    "bienes": {
        "label": "Libro de Bienes de Inversión",
        "pendientes_subdir": "PENDIENTES/bienes",
        "sandbox_folder": "22_BIENES_INVERSION",
    },
}


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

def get_output_dir() -> Path:
    """Obtener directorio de output."""
    return Path(settings().output_path)


def load_json_file(path: Path) -> dict:
    """Cargar un archivo JSON de forma segura."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error loading JSON file {path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


def list_document_ids() -> list[str]:
    """Listar todos los documentos procesados."""
    output_dir = get_output_dir()
    if not output_dir.exists():
        return []

    # Cada documento es un subdirectorio
    # Acepta IDs alfanuméricos (ej: factura_001) según AGENTS.md glosario
    doc_ids = []
    for item in sorted(output_dir.iterdir(), key=lambda x: x.name):
        if item.is_dir():
            doc_ids.append(item.name)

    return doc_ids


def get_validation_result(doc_id: str) -> Optional[dict]:
    """Obtener resultado de validación de un documento."""
    output_dir = get_output_dir()
    validation_path = output_dir / doc_id / "resultado_validacion.json"
    
    if not validation_path.exists():
        return None
    
    return load_json_file(validation_path)


def get_all_artifacts(doc_id: str) -> dict:
    """Obtener todos los artefactos de un documento."""
    output_dir = get_output_dir()
    doc_dir = output_dir / doc_id

    if not doc_dir.exists():
        return {}

    artifacts = {}
    for json_file in doc_dir.glob("*.json"):
        artifacts[json_file.stem] = load_json_file(json_file)

    return artifacts


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
    """
    Buscar el archivo de factura (PDF/imagen) en PROCESADAS o INCIDENCIAS.
    
    Returns tuple of (ruta_del_archivo, nombre_archivo) si existe, None en caso contrario.
    """
    cfg = settings()
    
    # Buscar en ambos directorios posibles
    folders_to_check = [
        cfg.folder_procesadas,
        cfg.folder_incidencias
    ]
    
    for folder_name in folders_to_check:
        folder_path = cfg.get_folder_path(folder_name)
        if not os.path.exists(folder_path):
            continue
            
        # Buscar archivos con extensiones permitidas
        for ext in ALLOWED_INVOICE_EXTENSIONS:
            # Buscar por patrón: doc_id + extensión
            invoice_file = Path(folder_path) / f"{doc_id}{ext}"
            if invoice_file.exists():
                return (invoice_file, invoice_file.name)
            
            # Buscar archivos que contengan el doc_id en el nombre
            for file in Path(folder_path).glob(f"*{doc_id}*{ext}"):
                return (file, file.name)
    
    return None


# ──────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────

@app.get("/api/invoices")
def list_invoices():
    """
    Listar todas las facturas procesadas con su estado.

    Returns una lista de documentos con su decisión global y metadata básica.
    """
    now = time.time()
    if _invoices_cache and (now - _invoices_cache.get("ts", 0)) < _CACHE_TTL:
        return _invoices_cache["data"]

    doc_ids = list_document_ids()
    invoices = []

    for doc_id in doc_ids:
        validation = get_validation_result(doc_id)
        if validation:
            campos = validation.get("campos", {})
            invoices.append({
                "id": doc_id,
                "decision_global": validation.get("decision_global", "pendiente"),
                "timestamp": validation.get("fecha_ensamblado", ""),
                "nif_entidad": campos.get("nif_entidad", {}).get("valor_final", ""),
                "nombre_entidad": campos.get("nombre_entidad", {}).get("valor_final", ""),
                "numero_factura": campos.get("numero_factura", {}).get("valor_final", ""),
                "total_euros": campos.get("total_euros", {}).get("valor_final", 0),
            })

    result = {"invoices": invoices, "total": len(invoices)}
    _invoices_cache["data"] = result
    _invoices_cache["ts"] = now
    return result


@app.get("/api/invoices/{doc_id}")
def get_invoice(doc_id: str):
    """
    Obtener detalles completos de una factura específica.

    Incluye todos los campos validados, desglose fiscal y decisión.
    """
    validate_doc_id(doc_id)
    
    validation = get_validation_result(doc_id)
    
    if not validation:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")
    
    # Construir respuesta completa
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

    # Build response
    inv_file = find_invoice_file(doc_id)
    invoice_filename = inv_file[1] if inv_file else None

    return {
        "id": doc_id,
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
        "artifacts": list(get_all_artifacts(doc_id).keys()),
        "invoice_filename": invoice_filename,
    }


@app.get("/api/invoices/{doc_id}/artifacts/{artifact_name}")
def get_artifact(doc_id: str, artifact_name: str):
    """
    Obtener un artefacto específico de un documento.

    Artefactos disponibles:
    - raw_document_ai
    - documento_extraido
    - resultado_identidad_cabecera
    - resultado_fiscal
    - resultado_semantica
    - resultado_cliente
    - resultado_validacion
    """
    validate_doc_id(doc_id)
    validate_artifact_name(artifact_name)
    
    output_dir = get_output_dir()
    artifact_path = output_dir / doc_id / f"{artifact_name}.json"
    
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

    Esta acción registra la decisión del usuario en un log de auditoría.
    """
    validate_doc_id(doc_id)
    
    validation = get_validation_result(doc_id)
    if not validation:
        raise HTTPException(status_code=404, detail=f"Invoice {doc_id} not found")
    
    # Registrar acción en archivo de estado
    output_dir = get_output_dir()
    action_log_path = output_dir / doc_id / "action_log.json"
    
    action_record = {
        "action": action.action,
        "document_id": doc_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "notes": action.notes,
        "decision_global_original": validation.get("decision_global"),
        "corrections_fields": action.corrections_fields,
        "corrections_fiscal_lines": action.corrections_fiscal_lines,
    }
    
    # Guardar log de acción
    try:
        action_log = []
        if action_log_path.exists():
            action_log = load_json_file(action_log_path)
        action_log.append(action_record)
        
        with open(action_log_path, "w", encoding="utf-8") as f:
            json.dump(action_log, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error(f"Error saving action log: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error saving action: {str(e)}")
    
    # Mover archivo de factura según acción del usuario
    file_moved = False
    if action.action == "approve":
        # Mover de 99_INCIDENCIAS a 90_PROCESADAS
        cfg = settings()
        result = find_invoice_file(doc_id)
        if result:
            invoice_file, filename = result
            dest_folder = cfg.get_folder_path(cfg.folder_procesadas)
            os.makedirs(dest_folder, exist_ok=True)
            dest_path = os.path.join(dest_folder, filename)
            try:
                shutil.move(str(invoice_file), dest_path)
                file_moved = True
                logger.info(f"Invoice {doc_id} moved to {dest_folder}")
            except OSError as e:
                logger.error(f"Error moving invoice file for {doc_id}: {e}", exc_info=True)

    return {
        "status": "success",
        "message": f"Action '{action.action}' recorded for invoice {doc_id}",
        "action": action_record,
        "file_moved": file_moved,
    }


@app.get("/api/stats")
def get_stats():
    """
    Obtener estadísticas generales del pipeline.

    Returns two dimensions separately:
    - by_decision: pipeline automatic decisions (auto/warn/pendiente/block)
    - by_user_action: human review actions (approved/rejected)
    """
    now = time.time()
    if _stats_cache and (now - _stats_cache.get("ts", 0)) < _CACHE_TTL:
        return _stats_cache["data"]

    doc_ids = list_document_ids()

    stats = {
        "total": len(doc_ids),
        "by_decision": {
            "auto": 0,
            "warn": 0,
            "pendiente": 0,
            "block": 0,
        },
        "by_user_action": {
            "approved": 0,
            "rejected": 0,
        },
    }

    for doc_id in doc_ids:
        validation = get_validation_result(doc_id)
        if validation:
            decision = validation.get("decision_global", "pendiente")
            if decision in stats["by_decision"]:
                stats["by_decision"][decision] += 1

        # Verificar si tiene acciones registradas
        output_dir = get_output_dir()
        action_log_path = output_dir / doc_id / "action_log.json"
        if action_log_path.exists():
            try:
                action_log = load_json_file(action_log_path)
                for action in action_log:
                    if action.get("action") == "approve":
                        stats["by_user_action"]["approved"] += 1
                    elif action.get("action") == "reject":
                        stats["by_user_action"]["rejected"] += 1
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[stats] Error leyendo action_log de %s: %s", doc_id, exc)

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
# Pipeline lock (persistido en disco) + cancel flag
# ──────────────────────────────────────────────────────────

def _lock_path() -> Path:
    return Path(settings().output_path) / ".pipeline_lock"


def _cancel_path() -> Path:
    return Path(settings().output_path) / ".pipeline_cancel"


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
        cfg = settings()
        status_path = Path(cfg.output_path) / "pipeline_status.json"
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
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("[startup] Error en recovery de pipeline: %s", exc, exc_info=True)


# ──────────────────────────────────────────────────────────
# New endpoints: books, upload, pipeline, clients
# ──────────────────────────────────────────────────────────

@app.get("/api/books")
def list_books():
    """List accounting books with pending files."""
    cfg = settings()
    sandbox_base = cfg.sandbox_base_path
    books = []
    for book_id, config in BOOK_CONFIGS.items():
        folder_path = os.path.join(sandbox_base, config["pendientes_subdir"])
        files = []
        if os.path.isdir(folder_path):
            for entry in os.scandir(folder_path):
                if entry.is_file():
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size_kb": round(stat.st_size / 1024, 1),
                        "added": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    })
        books.append({
            "id": book_id,
            "label": config["label"],
            "folder": folder_path,
            "files": sorted(files, key=lambda f: f["added"]),
        })
    return {"books": books}


@app.post("/api/books/{book_id}/upload")
async def upload_files(book_id: str, files: List[UploadFile] = File(...)):
    """Upload invoice files to a book's pending folder."""
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}. Valid: {list(BOOK_CONFIGS.keys())}")

    cfg = settings()
    upload_dir = os.path.join(cfg.sandbox_base_path, BOOK_CONFIGS[book_id]["pendientes_subdir"])
    os.makedirs(upload_dir, exist_ok=True)

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

        # Fix #1: Sanitize filename to prevent path traversal
        safe_name = pathlib.PurePosixPath(file.filename).name
        if not safe_name or safe_name.startswith(".") or ".." in safe_name:
            errors.append({"file": file.filename, "error": "Invalid filename"})
            continue

        dest = os.path.join(upload_dir, safe_name)

        # Fix #11: Avoid silent overwrite if file already exists
        if os.path.exists(dest):
            base, ext_part = os.path.splitext(safe_name)
            ts = int(datetime.now(timezone.utc).timestamp())
            safe_name = f"{base}_{ts}{ext_part}"
            dest = os.path.join(upload_dir, safe_name)

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
    """Delete a pending file before pipeline starts."""
    if book_id not in BOOK_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Invalid book_id: {book_id}")

    # Check pipeline is not running
    cfg = settings()
    status_path = os.path.join(cfg.output_path, "pipeline_status.json")
    if os.path.exists(status_path):
        try:
            with open(status_path) as f:
                status = json.load(f)
            if status.get("status") == "running":
                raise HTTPException(status_code=409, detail="Pipeline is running — cannot delete files")
        except (json.JSONDecodeError, KeyError):
            pass

    # Sanitize filename to prevent path traversal
    safe_name = pathlib.PurePosixPath(filename).name
    if not safe_name or safe_name.startswith(".") or ".." in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = os.path.join(cfg.sandbox_base_path, BOOK_CONFIGS[book_id]["pendientes_subdir"], safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    os.remove(file_path)
    return {"deleted": safe_name}


@app.get("/api/pipeline/status")
def pipeline_status():
    """Read pipeline processing status."""
    cfg = settings()
    status_path = os.path.join(cfg.output_path, "pipeline_status.json")
    if not os.path.exists(status_path):
        return {
            "status": "idle",
            "processed": 0,
            "total": 0,
            "current_file": None,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
        }
    try:
        with open(status_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading pipeline status: {e}", exc_info=True)
        return {"status": "error", "error_message": str(e)}


@app.post("/api/pipeline/run")
async def run_pipeline_endpoint():
    """Launch pipeline processing in background."""
    if not acquire_pipeline_lock():
        raise HTTPException(status_code=409, detail="Pipeline already running")

    cfg = settings()
    sandbox_base = cfg.sandbox_base_path

    # Count files across all PENDIENTES subfolders
    total_files = 0
    book_file_map = {}  # book_id -> list of file paths

    for book_id, config in BOOK_CONFIGS.items():
        pendientes_path = os.path.join(sandbox_base, config["pendientes_subdir"])
        if not os.path.isdir(pendientes_path):
            continue

        files = [os.path.join(pendientes_path, f) for f in os.listdir(pendientes_path) if os.path.isfile(os.path.join(pendientes_path, f))]
        if files:
            book_file_map[book_id] = files
            total_files += len(files)

    if total_files == 0:
        release_pipeline_lock()
        return {"status": "started", "total": 0}

    # Write initial status
    status_path = os.path.join(cfg.output_path, "pipeline_status.json")
    os.makedirs(cfg.output_path, exist_ok=True)
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
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(initial_status, f, indent=2)
    except Exception as e:
        logger.error(f"Error writing pipeline status: {e}", exc_info=True)

    # Launch in background — use run_in_executor to isolate from event loop
    def _run_pipeline_sync():
        try:
            # Inicializar logging del pipeline (JSONL + console)
            # Necesario porque la ruta API no pasa por pipeline.main()
            from src.logging_config import setup_logging
            setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

            # Import pipeline module
            from src.pipeline import run_pipeline

            # Persistir manifiesto de archivos para soporte de reset
            manifest = {"files": {}, "created_at": datetime.now(timezone.utc).isoformat()}
            for bid, fps in book_file_map.items():
                for fp in fps:
                    manifest["files"][os.path.basename(fp)] = {"original_book": bid}
            manifest_path = os.path.join(cfg.output_path, "file_manifest.json")
            os.makedirs(cfg.output_path, exist_ok=True)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
            logger.info("[pipeline] File manifest written: %d files", len(manifest["files"]))

            for book_id, file_paths in book_file_map.items():
                if is_cancel_requested():
                    logger.warning("[pipeline] Cancelación solicitada — abortando libros pendientes")
                    break

                sandbox_folder = os.path.join(sandbox_base, BOOK_CONFIGS[book_id]["sandbox_folder"])
                os.makedirs(sandbox_folder, exist_ok=True)

                # Copy files from PENDIENTES to sandbox
                for fp in file_paths:
                    shutil.copy2(fp, sandbox_folder)

                # Run pipeline for this book
                run_pipeline(sandbox_folder, BOOK_CONFIGS[book_id]["sandbox_folder"], status_file=status_path)

                # Clean up: remove processed files from PENDIENTES
                for fp in file_paths:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass

            # Update status on completion
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    status = json.load(f)
                if is_cancel_requested():
                    status["status"] = "cancelled"
                    status["error_message"] = "Procesamiento cancelado por el usuario"
                else:
                    status["status"] = "completed"
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                with open(status_path, "w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Pipeline background: error escribiendo status final: %s", exc, exc_info=True)
        except Exception as e:
            logger.error(f"Pipeline background task failed: {e}", exc_info=True)
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    status = json.load(f)
                status["status"] = "error"
                status["error_message"] = str(e)
                status["completed_at"] = datetime.now(timezone.utc).isoformat()
                with open(status_path, "w", encoding="utf-8") as f:
                    json.dump(status, f, indent=2)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Pipeline background: error escribiendo status de error: %s", exc, exc_info=True)
        finally:
            release_pipeline_lock()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_pipeline_sync)

    return {"status": "started", "total": total_files}


# ──────────────────────────────────────────────────────────
# Reset endpoint — devuelve archivos a PENDIENTES, limpia artefactos
# ──────────────────────────────────────────────────────────

# Reverse lookup: sandbox_folder → book_id
_SANDBOX_TO_BOOK = {v["sandbox_folder"]: k for k, v in BOOK_CONFIGS.items()}


@app.post("/api/pipeline/reset")
def reset_pipeline():
    """
    Reset completo del pipeline:
    1. Devuelve archivos procesados a sus carpetas PENDIENTES originales
    2. Limpia artefactos de output/
    3. Registra evento en audit log (trazabilidad)
    """
    if is_pipeline_locked():
        raise HTTPException(status_code=409, detail="Pipeline en ejecución. No se puede resetear.")

    cfg = settings()
    sandbox_base = cfg.sandbox_base_path
    output_path = Path(cfg.output_path)

    files_restored = 0
    doc_ids_deleted = 0

    # 1. Leer manifiesto de archivos (si existe)
    manifest_path = output_path / "file_manifest.json"
    manifest_files = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest_data = json.load(f)
            manifest_files = manifest_data.get("files", {})
        except Exception as e:
            logger.warning("[reset] Error leyendo file_manifest.json: %s", e)

    # 2. Escanear carpetas de destino y devolver archivos a PENDIENTES
    # Carpetas donde pueden estar los archivos procesados
    scan_folders = [
        cfg.get_folder_path(cfg.folder_procesadas),      # 90_PROCESADAS
        cfg.get_folder_path(cfg.folder_incidencias),      # 99_INCIDENCIAS
    ]
    # También escanear carpetas sandbox (por si pipeline crasheó a mitad)
    for book_id, config in BOOK_CONFIGS.items():
        sandbox_folder = os.path.join(sandbox_base, config["sandbox_folder"])
        if sandbox_folder not in scan_folders:
            scan_folders.append(sandbox_folder)

    for folder in scan_folders:
        if not os.path.isdir(folder):
            continue
        for entry in os.scandir(folder):
            if not entry.is_file():
                continue
            fname = entry.name
            # Determinar book_id original
            original_book = None
            if fname in manifest_files:
                original_book = manifest_files[fname].get("original_book")
            else:
                # Inferir desde carpeta sandbox si aplica
                folder_basename = os.path.basename(folder)
                if folder_basename in _SANDBOX_TO_BOOK:
                    original_book = _SANDBOX_TO_BOOK[folder_basename]

            if original_book and original_book in BOOK_CONFIGS:
                dest = os.path.join(sandbox_base, BOOK_CONFIGS[original_book]["pendientes_subdir"])
                os.makedirs(dest, exist_ok=True)
                try:
                    shutil.move(entry.path, os.path.join(dest, fname))
                    files_restored += 1
                    logger.info("[reset] Archivo devuelto: %s → %s", fname, dest)
                except OSError as e:
                    logger.error("[reset] Error moviendo %s: %s", fname, e, exc_info=True)
            else:
                logger.warning("[reset] Archivo %s sin book_id conocido, ignorado", fname)

    # 3. Borrar subcarpetas doc_id de output/
    if output_path.exists():
        for entry in output_path.iterdir():
            if entry.is_dir():
                try:
                    shutil.rmtree(entry)
                    doc_ids_deleted += 1
                except OSError as e:
                    logger.error("[reset] Error borrando %s: %s", entry, e, exc_info=True)

    # 4. Borrar pipeline_status.json y file_manifest.json
    for fname in ("pipeline_status.json", "file_manifest.json"):
        fpath = output_path / fname
        if fpath.exists():
            try:
                fpath.unlink()
            except OSError:
                pass

    # 5. Registrar evento en audit log (trazabilidad)
    try:
        audit_dir = Path(cfg.logs_path) / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        reset_record = {
            "schema_v": 1,
            "ts_proceso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "reset",
            "files_restored": files_restored,
            "doc_ids_deleted": doc_ids_deleted,
        }
        reset_log = audit_dir / f"reset_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        with open(reset_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(reset_record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("[reset] Error escribiendo audit log: %s", e, exc_info=True)

    logger.info("[reset] Completado: %d archivos restaurados, %d doc_ids eliminados", files_restored, doc_ids_deleted)

    return {
        "status": "ok",
        "files_restored": files_restored,
        "doc_ids_deleted": doc_ids_deleted,
    }


@app.get("/api/clients")
def list_clients():
    """List registered clients from clients.json."""
    cfg = settings()
    # clients.json is in data/ relative to project root (parent of output_path's parent)
    project_root = Path(cfg.output_path).parent
    clients_path = project_root / "data" / "clients.json"

    if not clients_path.exists():
        return {"clients": []}

    try:
        with open(clients_path, encoding="utf-8") as f:
            clients = json.load(f)
        return {"clients": clients}
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Error reading clients file: {e}", exc_info=True)
        return {"clients": []}


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
    output_dir = get_output_dir()
    status_path = output_dir / "pipeline_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("state") == "running":
                raise HTTPException(
                    status_code=409,
                    detail="No se puede actualizar mientras se procesan facturas. Espera a que termine el pipeline.",
                )
        except (json.JSONDecodeError, OSError):
            pass

    if not WATCHTOWER_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Servicio de actualizaciones no configurado (falta WATCHTOWER_HTTP_API_TOKEN).",
        )

    try:
        resp = httpx.post(
            f"{WATCHTOWER_URL}/v1/update",
            headers={"Authorization": f"Bearer {WATCHTOWER_TOKEN}"},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        logger.error("[system] Watchtower unreachable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="No se pudo contactar con el servicio de actualizaciones.",
        )
    except httpx.HTTPStatusError as exc:
        logger.error("[system] Watchtower returned %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"El servicio de actualizaciones devolvió error {exc.response.status_code}.",
        )

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
