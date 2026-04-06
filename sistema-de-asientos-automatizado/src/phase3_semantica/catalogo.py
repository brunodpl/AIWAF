"""
Carga y búsqueda en catálogos de semántica contable.

Lee catalogo_semantica.yaml y maestro_proveedores.yaml para:
  1. Lookup por NIF proveedor → concepto + cuenta por defecto
  2. Matching por patrones de texto → concepto + cuenta
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("pipeline.semantica.catalogo")


def _normalizar_texto(texto: str) -> str:
    """Normalizar texto para matching: mayúsculas, sin tildes, sin puntuación extra."""
    texto = texto.upper().strip()
    # Eliminar tildes
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    # Colapsar espacios
    texto = re.sub(r"\s+", " ", texto)
    return texto


def cargar_catalogo_semantica(path: str | Path) -> list[dict]:
    """
    Cargar catalogo_semantica.yaml.

    Returns:
        Lista de entradas con patrones normalizados para búsqueda rápida.
        Cada entrada: {patrones_norm: [...], concepto, cuentacontable, confianza_catalogo}
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"[catalogo] No encontrado: {path}")
        return []

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, list):
        logger.warning("[catalogo] catalogo_semantica.yaml no es una lista")
        return []

    entradas = []
    for item in raw:
        patrones = item.get("patrones", [])
        entradas.append({
            "patrones_raw": patrones,
            "patrones_norm": [_normalizar_texto(p) for p in patrones],
            "concepto": item.get("concepto"),
            "cuentacontable": item.get("cuentacontable"),
            "confianza_catalogo": item.get("confianza_catalogo", 0.90),
        })

    logger.info(f"[catalogo] Cargadas {len(entradas)} entradas de catálogo semántico")
    return entradas


def cargar_maestro_proveedores(path: str | Path) -> dict[str, dict]:
    """
    Cargar maestro_proveedores.yaml.

    Returns:
        Dict NIF → {nombre, concepto_defecto, cuentacontable_defecto, confianza}
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"[catalogo] No encontrado: {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    proveedores = raw.get("proveedores", []) if isinstance(raw, dict) else []
    result = {}
    for p in proveedores:
        nif = p.get("nif", "").strip().upper()
        if nif:
            result[nif] = {
                "nombre": p.get("nombre", ""),
                "concepto_defecto": p.get("concepto_defecto", ""),
                "cuentacontable_defecto": p.get("cuentacontable_defecto"),
                "confianza": p.get("confianza", 0.90),
            }

    logger.info(f"[catalogo] Cargados {len(result)} proveedores del maestro")
    return result


def buscar_por_nif(
    nif: str,
    proveedores: dict[str, dict],
) -> Optional[dict]:
    """
    Lookup por NIF en maestro de proveedores.

    Returns:
        {concepto, cuentacontable, confianza, fuente} o None
    """
    nif_norm = nif.strip().upper() if nif else ""
    prov = proveedores.get(nif_norm)
    if prov and prov.get("cuentacontable_defecto"):
        return {
            "concepto": prov["concepto_defecto"],
            "cuentacontable": prov["cuentacontable_defecto"],
            "confianza": prov["confianza"],
            "fuente": "maestro_proveedores",
        }
    return None


def buscar_por_patrones(
    texto_ocr: str,
    catalogo: list[dict],
) -> list[dict]:
    """
    Matching por keywords contra catálogo de patrones.

    Returns:
        Lista de candidatos ordenados por confianza descendente.
        Cada candidato: {concepto, cuentacontable, confianza, fuente, patron_matched}
    """
    if not texto_ocr or not catalogo:
        return []

    texto_norm = _normalizar_texto(texto_ocr)
    candidatos = []

    for entrada in catalogo:
        for patron_norm, patron_raw in zip(
            entrada["patrones_norm"], entrada["patrones_raw"]
        ):
            if patron_norm in texto_norm:
                candidatos.append({
                    "concepto": entrada["concepto"],
                    "cuentacontable": entrada["cuentacontable"],
                    "confianza": entrada["confianza_catalogo"],
                    "fuente": "catalogo_semantica",
                    "patron_matched": patron_raw,
                })
                break  # Un match por entrada es suficiente

    # Ordenar por confianza descendente
    candidatos.sort(key=lambda c: c["confianza"], reverse=True)
    return candidatos
