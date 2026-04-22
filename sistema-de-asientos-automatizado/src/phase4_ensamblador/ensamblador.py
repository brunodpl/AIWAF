"""
Fase 4: Ensamblado y decisión final.

Lee artefactos de fase 2 y fase 3.x desde disco.
Escribe resultado_validacion.json con trazabilidad completa.

Extensibilidad: añadir un módulo nuevo = sólo actualizar schema.py.
Este fichero no cambia al añadir fiscal, semántica o cliente.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schema import (
    MODULOS_FASE3,
    ARTEFACTO_OCR,
    CAMPOS_POR_MODULO,
    CAMPOS_OBLIGATORIOS,
    CAMPO_PENDIENTE,
    CAMPO_ERROR,
)

logger = logging.getLogger("pipeline.ensamblador")


# ──────────────────────────────────────────────────────────
# Detección de artefactos en disco
# ──────────────────────────────────────────────────────────

def _cargar_artefacto(path: Path, nombre_artefacto: str) -> dict:
    """Cargar un artefacto JSON. Devuelve estado ok/pendiente/error."""
    if not path.exists():
        return {"estado": "pendiente", "data": None, "artefacto": None}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"estado": "ok", "data": data, "artefacto": nombre_artefacto}
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[ensamblador] Artefacto corrupto {path}: {e}", exc_info=True)
        return {"estado": "error", "data": None, "artefacto": nombre_artefacto}


def _detectar_modulos(doc_dir: Path) -> dict:
    """
    Lee qué artefactos de fase 2 y fase 3 existen en disco y carga su contenido.

    Returns:
        {
          "ocr":                 {"estado": "ok", "data": {...}, "artefacto": "..."},
          "identidad_cabecera":  {"estado": "ok", "data": {...}, "artefacto": "..."},
          "fiscal":              {"estado": "pendiente", "data": None, "artefacto": None},
          ...
        }
    """
    modulos = {}

    # OCR (fase 2)
    modulos["ocr"] = _cargar_artefacto(doc_dir / ARTEFACTO_OCR, ARTEFACTO_OCR)

    # Módulos fase 3
    for nombre, artefacto in MODULOS_FASE3.items():
        modulos[nombre] = _cargar_artefacto(doc_dir / artefacto, artefacto)

    return modulos


# ──────────────────────────────────────────────────────────
# Limpieza de valores
# ──────────────────────────────────────────────────────────

# Símbolos que el OCR devuelve cuando no encuentra un valor
_PLACEHOLDERS_VACIOS = {"-", "+", "%", "--", "N/A", "n/a", "na", "NA", ""}


def _limpiar_valor(valor):
    """
    Elimina símbolos inválidos de valor_final antes de ensamblar.

    - Strings que son solo un placeholder ("-", "N/A"…) → None
    - Strings con signo delantero (+/-/%) → se elimina el signo
    - Listas, dicts, None y valores no-string → sin cambios
    """
    if valor is None or not isinstance(valor, str):
        return valor
    stripped = valor.strip()
    if stripped in _PLACEHOLDERS_VACIOS:
        return None
    if stripped and stripped[0] in ("+", "-", "%"):
        stripped = stripped[1:].strip()
        return stripped if stripped else None
    return stripped


# ──────────────────────────────────────────────────────────
# Traducción de formato de módulo a formato ensamblador
# ──────────────────────────────────────────────────────────

def _extraer_campo_de_modulo(modulo_data: dict, campo: str, nombre_modulo: str) -> dict:
    """
    Traduce el campo desde el JSON del módulo al formato uniforme del ensamblador.

    Identidad produce:
        {valor_final, fuente_final, confianza_final, decision, motivo, llm_usado, ...}
    Ensamblador espera:
        {valor_final, fuente_modulo, fuente_dato, confianza, decision, llm_usado, motivo, ocr_fallback}
    """
    campos_modulo = modulo_data.get("campos", {})
    campo_data = campos_modulo.get(campo)

    if campo_data is None:
        # El módulo existe pero no tiene este campo (no debería ocurrir en producción)
        plantilla = deepcopy(CAMPO_PENDIENTE)
        plantilla["motivo"] = f"Campo '{campo}' no encontrado en módulo {nombre_modulo}."
        plantilla["fuente_modulo"] = nombre_modulo
        return plantilla

    # Detectar si el LLM fue usado para este campo específico
    llm_usado = False
    candidatos = campo_data.get("candidatos", [])
    for c in candidatos:
        if c.get("fuente") == "llm_arbitro":
            llm_usado = True
            break
    # Fallback: flag global del módulo o validaciones
    if not llm_usado:
        llm_usado = campo_data.get("validaciones", {}).get("llm_usado", False)
    if not llm_usado:
        llm_usado = modulo_data.get("llm_usado", False)

    # Mapeo de claves: identidad usa fuente_final/confianza_final, ensamblador usa fuente_dato/confianza
    return {
        "valor_final":   _limpiar_valor(campo_data.get("valor_final")),
        "fuente_modulo": nombre_modulo,
        "fuente_dato":   campo_data.get("fuente_final"),
        "confianza":     campo_data.get("confianza_final"),
        "decision":      campo_data.get("decision", "block"),
        "llm_usado":     llm_usado,
        "motivo":        campo_data.get("motivo", ""),
        "ocr_fallback":  campo_data.get("fuente_final") == "fallback",
    }


# ──────────────────────────────────────────────────────────
# Ensamblado de campos
# ──────────────────────────────────────────────────────────

def _ensamblar_campos(modulos: dict) -> dict:
    """
    Para cada campo del asiento final, obtener el valor del módulo responsable
    o marcarlo como pendiente/error si el módulo no está disponible.
    """
    campos = {}
    for campo, nombre_modulo in CAMPOS_POR_MODULO.items():
        info_modulo = modulos.get(nombre_modulo, {"estado": "pendiente", "data": None})
        estado = info_modulo["estado"]

        if estado == "ok" and info_modulo["data"] is not None:
            campos[campo] = _extraer_campo_de_modulo(
                info_modulo["data"], campo, nombre_modulo
            )
        elif estado == "error":
            plantilla = deepcopy(CAMPO_ERROR)
            plantilla["motivo"] = plantilla["motivo"].format(modulo=nombre_modulo)
            campos[campo] = plantilla
        else:  # pendiente
            plantilla = deepcopy(CAMPO_PENDIENTE)
            plantilla["motivo"] = plantilla["motivo"].format(modulo=nombre_modulo)
            campos[campo] = plantilla

    return campos


# ──────────────────────────────────────────────────────────
# Decisión global
# ──────────────────────────────────────────────────────────

# Prioridad: block > warn > pendiente > auto
_PRIORIDAD_DECISION = {"block": 3, "warn": 2, "pendiente": 1, "auto": 0}


def _calcular_decision_global(campos: dict) -> tuple[str, bool, list[str]]:
    """
    Calcula la decisión global como la peor decisión entre campos obligatorios.

    Returns:
        (decision_global, autocargable, motivos_revision)
    """
    peor = "auto"
    motivos = []

    for campo in CAMPOS_OBLIGATORIOS:
        campo_data = campos.get(campo, {})
        decision = campo_data.get("decision", "block")
        prioridad_actual = _PRIORIDAD_DECISION.get(decision, 3)
        prioridad_peor = _PRIORIDAD_DECISION.get(peor, 0)

        if prioridad_actual > prioridad_peor:
            peor = decision

        if decision != "auto":
            motivo = campo_data.get("motivo", f"Campo {campo}: {decision}")
            motivos.append(f"{campo}: {motivo}")

    autocargable = (peor == "auto")
    return peor, autocargable, motivos


# ──────────────────────────────────────────────────────────
# Verificaciones cruzadas
# ──────────────────────────────────────────────────────────

def _verificaciones_cruzadas(campos: dict, modulos: dict) -> dict:
    """
    Verificaciones entre módulos. Devuelve null si el módulo no está disponible.
    Se calculan aquí porque requieren datos de múltiples fases.
    """
    verificaciones: dict[str, Optional[bool]] = {
        "suma_fiscal_correcta":       None,  # Requiere módulo fiscal
        "fecha_expedicion_no_futura": None,
        "nif_formato_valido":         None,
        "tipo_iva_en_catalogo":       None,  # Requiere módulo fiscal
    }

    # fecha_expedicion_no_futura: disponible si identidad ok
    if modulos.get("identidad_cabecera", {}).get("estado") == "ok":
        fecha_exp = campos.get("fecha_expedicion", {}).get("valor_final")
        fecha_oper = campos.get("fecha_operacion", {}).get("valor_final")
        if fecha_exp and fecha_oper:
            try:
                verificaciones["fecha_expedicion_no_futura"] = fecha_exp <= fecha_oper
            except Exception:
                verificaciones["fecha_expedicion_no_futura"] = None

    # nif_formato_valido: disponible si identidad ok y nif presente
    if modulos.get("identidad_cabecera", {}).get("estado") == "ok":
        nif_data = campos.get("nif_entidad", {})
        if nif_data.get("valor_final") is not None:
            # Si llegó con decision auto o warn, el checksum ya fue validado por identidad
            decision_nif = nif_data.get("decision", "block")
            verificaciones["nif_formato_valido"] = decision_nif in ("auto", "warn")

    return verificaciones


# ──────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "v1"


def run_ensamblador(documento_id: str, doc_output_dir: str, libro: str) -> bool:
    """
    Ensamblar resultado_validacion.json para un documento.

    Lee todos los artefactos de fase 3 disponibles en doc_output_dir,
    construye el JSON final y lo escribe en el mismo directorio.

    Args:
        documento_id: ID del documento (basename del archivo original)
        doc_output_dir: Directorio con los artefactos de fase 2 y 3
        libro: Libro contable de destino (e.g., "20_COMPRAS_GASTOS")

    Returns:
        True si el ensamblado completó correctamente
        False si hubo error técnico
    """
    doc_dir = Path(doc_output_dir)
    output_path = doc_dir / "resultado_validacion.json"

    logger.info(f"[ensamblador] Iniciando para doc_id={documento_id}")

    try:
        # 1. Detectar qué módulos tienen artefactos en disco
        modulos = _detectar_modulos(doc_dir)

        # 2. Ensamblar campos del asiento
        campos = _ensamblar_campos(modulos)

        # 3. Calcular decisión global
        decision_global, autocargable, motivos_revision = _calcular_decision_global(campos)

        # 4. Verificaciones cruzadas
        verificaciones = _verificaciones_cruzadas(campos, modulos)

        # 5. Construir resultado final
        resultado = {
            "documento_id":    documento_id,
            "schema_version":  SCHEMA_VERSION,
            "fecha_ensamblado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "libro":           libro,
            "modulos": {
                nombre: {"estado": info["estado"], "artefacto": info.get("artefacto")}
                for nombre, info in modulos.items()
            },
            "campos":          campos,
            "decision_global": decision_global,
            "autocargable":    autocargable,
            "motivos_revision": motivos_revision,
            "verificaciones":  verificaciones,
        }

        # 6. Escribir a disco
        doc_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[ensamblador] resultado_validacion.json escrito: "
            f"decision_global={decision_global} autocargable={autocargable}"
        )
        return True

    except Exception as e:
        logger.error(f"[ensamblador] Error técnico: {e}", exc_info=True)
        return False
