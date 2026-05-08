"""
Escritor del registro de auditoría de negocio.

Genera UN registro JSON por documento procesado en:
  logs/audit/{libro}_{fecha}.jsonl

Este fichero es el rastro auditable de qué hizo el pipeline con cada factura.
No es un log técnico: está pensado para ser leído por la gestoría y para
demostrar trazabilidad interna del proceso documental.

Retención recomendada: 4 años (alineada con plazo de prescripción fiscal
para conservación de facturas y justificantes, RD 1619/2012 y LGT art. 66).
La retención es responsabilidad operativa del equipo, no se automatiza aquí.

Formato: JSONL (una línea JSON por documento, fichero append-only).
Nunca se modifica un registro ya escrito: si hay corrección, se añade nuevo
registro con motivo de rectificación.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pipeline.audit")

# Versión del esquema del registro de auditoría.
# Incrementar si se añaden campos obligatorios o se cambia la estructura.
SCHEMA_VERSION = 1


class AuditWriter:
    """
    Escribe registros de auditoría de negocio para una ejecución del pipeline.

    Un registro por documento. Append-only.
    Instanciar una vez por ejecución de carpeta desde pipeline.run_pipeline().
    """

    def __init__(self, logs_path: str, libro: str) -> None:
        """
        Inicializar escritor para una ejecución concreta.

        Args:
            logs_path: Directorio donde aterriza el JSONL de auditoría.

                Trazabilidad 2.0: pasar directamente ``cfg.audit_path()``
                (ej. ``libros/logs/audit``). El parámetro conserva el nombre
                ``logs_path`` por compatibilidad con código y tests legacy.
                Si se pasa un directorio que no acaba en ``audit/``, se asume
                legacy y se anexa ``/audit`` automáticamente.
            libro: Nombre del libro contable (e.g. "20_COMPRAS_GASTOS").
        """
        audit_dir = Path(logs_path)
        if audit_dir.name != "audit":
            audit_dir = audit_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)

        fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._file_path = audit_dir / f"{libro}_{fecha_hoy}.jsonl"
        self._libro = libro
        logger.info(f"[audit] Registro de auditoría: {self._file_path}")

    def write(
        self,
        doc_id: Optional[str],
        file_path: str,
        results: list,
        decision: str,
        output_base_path: str,
        folder_name: Optional[str] = None,
    ) -> None:
        """
        Escribir un registro de auditoría para un documento procesado.

        Llama al finalizar cada documento en pipeline.run_pipeline().
        Si falla la escritura, lo registra en el log técnico pero NO
        propaga la excepción para no interrumpir el pipeline.

        Args:
            doc_id:           ID del documento (basename sin extensión). None si OCR falló.
            file_path:        Ruta original del fichero de entrada.
            results:          Lista de PhaseResult del pipeline.
            decision:         Decisión global ("auto", "warn", "pendiente", "block", "error").
            output_base_path: Raíz de carpetas de asiento (típicamente
                              ``cfg.asientos_path()``).
            folder_name:      Nombre actual de la carpeta de asiento dentro
                              de ``output_base_path``. Si se omite, se usa
                              ``doc_id`` (compatibilidad con la estructura
                              legacy ``data/output/{doc_id}/``).
        """
        try:
            record = self._build_record(
                doc_id, file_path, results, decision, output_base_path, folder_name
            )
            line = json.dumps(record, ensure_ascii=False)
            with open(self._file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            logger.info(
                f"[audit] Registro escrito: doc_id={doc_id or 'UNKNOWN'} "
                f"decision={decision}"
            )
        except Exception as e:
            # Nunca interrumpir el pipeline por un fallo de escritura de auditoría
            logger.error(
                f"[audit] ERROR al escribir registro para {doc_id or file_path}: {e}",
                exc_info=True,
            )

    def _build_record(
        self,
        doc_id: Optional[str],
        file_path: str,
        results: list,
        decision: str,
        output_base_path: str,
        folder_name: Optional[str] = None,
    ) -> dict:
        """
        Construir el dict del registro de auditoría.

        Lee resultado_validacion.json si existe para extraer campos críticos
        y motivos de revisión. Si no existe, los marca como no disponibles.
        """
        import os

        archivo_origen = os.path.basename(file_path)

        # ── Fases ejecutadas ──────────────────────────────────────────────
        fases: dict[str, dict] = {}
        for r in results:
            fases[r.fase] = {
                "ok":    r.ok,
                "motivo": r.motivo if r.motivo else None,
            }

        # ── Localizar carpeta del documento ───────────────────────────────
        # Trazabilidad 2.0: la carpeta puede tener nombre distinto al doc_id
        # (libro_doc_id en provisional, esquema descriptivo tras rename).
        # Preferir folder_name si se proporciona, fallback a doc_id (legacy).
        carpeta_nombre = folder_name or doc_id

        # ── Leer resultado_validacion.json para campos y motivos ──────────
        campos_criticos: dict = {}
        motivos_revision: list = []
        verificaciones: dict = {}

        if carpeta_nombre:
            validacion = _leer_resultado_validacion(carpeta_nombre, output_base_path)
            if validacion:
                campos_criticos = _extraer_campos_criticos(validacion)
                motivos_revision = validacion.get("motivos_revision", [])
                verificaciones   = validacion.get("verificaciones", {})

        # ── Artefactos generados ──────────────────────────────────────────
        artefactos: dict[str, Optional[str]] = {"resultado_validacion": None}
        if carpeta_nombre:
            base = Path(output_base_path) / carpeta_nombre
            artefactos = {
                "documento_extraido":           str(base / "documento_extraido.json"),
                "resultado_identidad_cabecera": str(base / "resultado_identidad_cabecera.json"),
                "resultado_fiscal":             str(base / "resultado_fiscal.json"),
                "resultado_semantica":          str(base / "resultado_semantica.json"),
                "resultado_cliente":            str(base / "resultado_cliente.json"),
                "resultado_validacion":         str(base / "resultado_validacion.json"),
                "state":                        str(base / ".state.json"),
            }

        return {
            "schema_v":        SCHEMA_VERSION,
            "ts_proceso":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doc_id":          doc_id or "UNKNOWN",
            "folder_name":     folder_name,
            "libro":           self._libro,
            "archivo_origen":  archivo_origen,
            "fases":           fases,
            "campos_criticos": campos_criticos,
            "decision_global": decision,
            "autocargable":    decision == "auto",
            "motivos_revision": motivos_revision,
            "verificaciones":  verificaciones,
            "artefactos":      artefactos,
        }


# ── Helpers privados ──────────────────────────────────────────────────────────

_CAMPOS_A_EXTRAER = (
    "nif_entidad",
    "nombre_entidad",
    "numero_factura",
    "fecha_expedicion",
    "fecha_operacion",
    "total_euros",
    "nif_receptor",
    "concepto",
    "nif_cliente",
    "nombre_cliente",
)


def _leer_resultado_validacion(carpeta: str, output_base_path: str) -> Optional[dict]:
    """Leer resultado_validacion.json. Devuelve None si no existe o está corrupto.

    ``carpeta`` es el nombre de la carpeta dentro de ``output_base_path``.
    Coincide con ``doc_id`` en la estructura legacy y con ``folder_name``
    en la estructura nueva (libros/asientos/{libro}_{doc_id}/).
    """
    path = Path(output_base_path) / carpeta / "resultado_validacion.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _extraer_campos_criticos(validacion: dict) -> dict:
    """
    Extraer resumen de campos críticos desde resultado_validacion.json.

    Solo incluye valor_final, confianza y decision para cada campo.
    No replica toda la estructura interna del ensamblador.
    """
    campos_raw = validacion.get("campos", {})
    resultado = {}
    for campo in _CAMPOS_A_EXTRAER:
        dato = campos_raw.get(campo, {})
        resultado[campo] = {
            "valor":     dato.get("valor_final"),
            "confianza": dato.get("confianza"),
            "decision":  dato.get("decision", "pendiente"),
        }
    return resultado
