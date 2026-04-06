"""
FastAPI application for the invoice processing pipeline.

Provides endpoints for:
- Listing processed invoices
- Getting invoice details (validation results)
- Approving/rejecting invoices from the UI
- Triggering pipeline processing
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from urllib.parse import quote

from src.config import settings

logger = logging.getLogger("pipeline.api")

app = FastAPI(
    title="Pipeline de Asientos Automatizados",
    description="API para gestionar el procesamiento de facturas del pipeline",
    version="1.0.0",
)

# CORS para permitir conexiones desde la interfaz Next.js
# En producción, restringir a origins específicos via variable de entorno
allowed_origins = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000"
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
    y tipos incompatibles sin lanzar excepción.
    """
    if val is None:
        return default
    try:
        return float(val)
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
    
    return {"invoices": invoices, "total": len(invoices)}


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
    
    # TODO: Implementar movimiento de carpetas según acción
    # Por ahora, solo registramos la acción
    
    return {
        "status": "success",
        "message": f"Action '{action.action}' recorded for invoice {doc_id}",
        "action": action_record,
    }


@app.get("/api/stats")
def get_stats():
    """
    Obtener estadísticas generales del pipeline.

    Returns two dimensions separately:
    - by_decision: pipeline automatic decisions (auto/warn/pendiente/block)
    - by_user_action: human review actions (approved/rejected)
    """
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
            except Exception:
                pass

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
