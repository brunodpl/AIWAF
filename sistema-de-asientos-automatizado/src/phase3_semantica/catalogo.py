"""
Carga y busqueda en catalogos de semantica contable.

Lee maestro_contable_fiscal.yaml (v3) y maestro_proveedores.yaml para:
  1. Lookup por NIF proveedor -> concepto + cuenta por defecto
  2. Filtrado de cuentas por libro para el prompt LLM
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("pipeline.semantica.catalogo")

# Mapeo libro -> campo "book" en el maestro v3
_LIBRO_A_BOOK = {
    "20_COMPRAS_GASTOS":   "compras_gastos",
    "21_VENTAS_INGRESOS":  "ingresos_ventas",
    "22_BIENES_INVERSION": "bienes_inversion",
}


def _normalizar_texto(texto: str) -> str:
    """Normalizar texto para matching: mayusculas, sin tildes, sin puntuacion extra."""
    texto = texto.upper().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"\s+", " ", texto)
    return texto


def cargar_maestro_contable(path: str | Path) -> dict:
    """
    Cargar maestro_contable_fiscal.yaml v3.

    Returns:
        Dict con claves: version, cuentas (list), conceptos (dict), clases_fiscales (dict).
        Dict vacio si el archivo no existe o falla la carga.
    """
    path = Path(path)
    if not path.exists():
        logger.warning(f"[catalogo] Maestro contable no encontrado: {path}")
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning("[catalogo] maestro_contable_fiscal.yaml no es un dict")
            return {}
        logger.info(
            f"[catalogo] Maestro contable v{data.get('version')} cargado: "
            f"{len(data.get('cuentas', []))} cuentas"
        )
        return data
    except Exception as e:
        logger.error(f"[catalogo] Error cargando maestro contable: {e}", exc_info=True)
        return {}


def cuentas_para_libro(maestro: dict, libro: str) -> list[dict]:
    """
    Filtrar cuentas del maestro segun el libro contable.

    Args:
        maestro: Dict cargado con cargar_maestro_contable()
        libro: e.g. "20_COMPRAS_GASTOS"

    Returns:
        Lista de dicts de cuenta {code, label, descripcion, human_review, ...}
        Lista vacia si libro desconocido o maestro vacio.
    """
    if not maestro:
        return []
    book_value = _LIBRO_A_BOOK.get(libro)
    if not book_value:
        logger.warning(f"[catalogo] Libro desconocido para filtrado: {libro}")
        return []
    return [c for c in maestro.get("cuentas", []) if c.get("book") == book_value]


def cargar_maestro_proveedores(path: str | Path) -> dict[str, dict]:
    """
    Cargar maestro_proveedores.yaml.

    Returns:
        Dict NIF -> {nombre, concepto_defecto, cuentacontable_defecto, confianza}
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


# ──────────────────────────────────────────────────────────
# Stubs de compatibilidad — seran eliminados en Task 3
# resolver.py aun los importa; no tienen logica de patrones
# ──────────────────────────────────────────────────────────

def cargar_catalogo_semantica(path: str | Path) -> list[dict]:
    """
    DEPRECATED — stub de compatibilidad hasta Task 3.
    El catalogo de patrones es reemplazado por el maestro contable v3 + LLM.
    """
    logger.warning("[catalogo] cargar_catalogo_semantica esta deprecado (LLM-first refactor)")
    return []


def buscar_por_patrones(texto_ocr: str, catalogo: list[dict]) -> list[dict]:
    """
    DEPRECATED — stub de compatibilidad hasta Task 3.
    El matching por patrones es reemplazado por LLM.
    """
    logger.warning("[catalogo] buscar_por_patrones esta deprecado (LLM-first refactor)")
    return []


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
