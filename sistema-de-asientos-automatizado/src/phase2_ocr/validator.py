"""
Validación de campos críticos en documento_extraido.json.

Verifica presencia y confianza mínima de los 5 campos críticos
requeridos según RD 1619/2012.
"""

import logging
from typing import Dict, Any, Tuple, List
from .config import settings as get_settings

logger = logging.getLogger("pipeline.ocr")


# Campos críticos mínimos (RD 1619/2012)
CAMPOS_CRITICOS = [
    ("identificacion", "numero_factura"),
    ("identificacion", "fecha_expedicion"),
    ("identificacion", "nombre_entidad"),
    ("identificacion", "nif_entidad"),
    ("fiscal",         "total_euros"),
]


def _get_total_euros(documento: Dict[str, Any]) -> Dict[str, Any]:
    null_result: Dict[str, Any] = {"valor": None, "confianza": None}
    fiscal = documento.get("fiscal", {})
    return fiscal.get("total_euros", null_result)


def get_nested_field(data: Dict[str, Any], section: str, field: str) -> Dict[str, Any]:
    if section == "fiscal" and field == "total_euros":
        return _get_total_euros(data)
    return data.get(section, {}).get(field, {"valor": None, "confianza": None})


def validate_documento_extraido(documento: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validar presencia y confianza de campos críticos.

    Reglas:
    - Todos los campos críticos deben tener valor no-null.
    - Todos los campos críticos deben tener confianza >= CONFIANZA_MINIMA.

    Los mensajes WARNING no incluyen los valores extraídos (RGPD):
    solo nombre del campo, decisión y confianza numérica.

    Args:
        documento: Dict con estructura de documento_extraido.json

    Returns:
        Tupla de (es_valido, lista_de_motivos_de_fallo)
    """
    cfg = get_settings()
    motivos: List[str] = []

    for section, field in CAMPOS_CRITICOS:
        field_data = get_nested_field(documento, section, field)
        valor = field_data.get("valor")
        confianza = field_data.get("confianza")

        if valor is None:
            motivos.append(f"Campo crítico ausente: {section}.{field}")
            logger.warning(
                f"[ocr] validacion: {section}.{field} ausente "
                f"doc_id={documento.get('documento_id', 'UNKNOWN')}"
            )
            continue

        if confianza is None or confianza < cfg.confianza_minima:
            motivos.append(
                f"Confianza insuficiente en {section}.{field}: "
                f"{confianza if confianza is not None else 'null'} "
                f"< {cfg.confianza_minima}"
            )
            logger.warning(
                f"[ocr] validacion: {section}.{field} "
                f"confianza={confianza} < umbral={cfg.confianza_minima} "
                f"doc_id={documento.get('documento_id', 'UNKNOWN')}"
            )

    is_valid = len(motivos) == 0

    if is_valid:
        logger.info(
            f"[ocr] validacion OK: todos los campos críticos "
            f"doc_id={documento.get('documento_id', 'UNKNOWN')}"
        )
    else:
        logger.warning(
            f"[ocr] validacion FALLO: {len(motivos)} campo(s) "
            f"doc_id={documento.get('documento_id', 'UNKNOWN')}"
        )

    return is_valid, motivos
