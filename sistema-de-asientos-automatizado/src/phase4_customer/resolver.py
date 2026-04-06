"""
Lógica determinista de resolución de cliente destino.

Dado el resultado de identidad y el libro de origen, determina quién es
el cliente de la gestoría para esta factura.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("pipeline.cliente_destino")


# ──────────────────────────────────────────────────────────
# Mapeo libro → campos de identidad que representan al cliente
# ──────────────────────────────────────────────────────────

LIBRO_A_ROL_CLIENTE: dict[str, tuple[str, str]] = {
    "20_COMPRAS_GASTOS":    ("nif_receptor",  "nombre_receptor"),
    "21_VENTAS_INGRESOS":   ("nif_entidad",   "nombre_entidad"),
    "22_BIENES_INVERSION":  ("nif_receptor",  "nombre_receptor"),
}


def _buscar_nombre_similar(maestro: dict, nombre: str) -> dict | None:
    """
    Busca coincidencia parcial por nombre en el maestro.

    Compara en mayúsculas y sin espacios extra.
    Retorna la entrada del maestro si hay match, None si no.
    """
    if not nombre:
        return None
    nombre_norm = nombre.strip().upper()
    clientes = maestro.get("clientes", {})
    for nif, datos in clientes.items():
        nombre_maestro = datos.get("nombre", "").strip().upper()
        if nombre_norm == nombre_maestro:
            return {"nif": nif, **datos}
    return None


def resolver_cliente(
    campos_identidad: dict,
    libro: str,
    maestro: dict,
    documento_id: str = "",
) -> dict:
    """
    Resuelve el cliente destino de la gestoría a partir de la identidad y el libro.

    Args:
        campos_identidad: dict con la estructura de resultado_identidad_cabecera.json["campos"]
        libro: libro contable de origen (e.g., "20_COMPRAS_GASTOS")
        maestro: dict del maestro de clientes cargado en memoria
        documento_id: ID del documento para trazabilidad

    Returns:
        dict compatible con el formato de artefacto del ensamblador
    """
    timestamp = datetime.now().isoformat(timespec="seconds")

    # Libro desconocido → block
    if libro not in LIBRO_A_ROL_CLIENTE:
        logger.warning(f"[cliente_destino] Libro desconocido: {libro}")
        return _resultado_block(
            documento_id, timestamp, libro,
            motivo=f"Libro desconocido: {libro}",
        )

    campo_nif, campo_nombre = LIBRO_A_ROL_CLIENTE[libro]

    # Extraer campos de identidad
    nif_data = campos_identidad.get(campo_nif, {})
    nombre_data = campos_identidad.get(campo_nombre, {})

    nif_valor = nif_data.get("valor_final")
    nif_confianza = nif_data.get("confianza_final", 0.0)
    nif_decision = nif_data.get("decision", "block")

    nombre_valor = nombre_data.get("valor_final")
    nombre_confianza = nombre_data.get("confianza_final", 0.0)
    nombre_decision = nombre_data.get("decision", "block")

    # Si el campo de identidad está en block o sin valor → propagar block
    if nif_decision == "block" or nif_valor is None:
        return _resultado_block(
            documento_id, timestamp, libro,
            motivo=f"Campo {campo_nif} no disponible (decision={nif_decision})",
            campo_origen_nif=campo_nif,
            campo_origen_nombre=campo_nombre,
        )

    # Determinar decisión del cliente y si es nuevo
    decision_nif = nif_decision
    decision_nombre = nombre_decision
    es_nuevo = True
    motivo_parts = []

    clientes = maestro.get("clientes", {})

    if nif_valor in clientes:
        # Match exacto por NIF
        es_nuevo = False
        motivo_parts.append(f"NIF {nif_valor} encontrado en maestro")
    else:
        # Buscar coincidencia por nombre
        match_nombre = _buscar_nombre_similar(maestro, nombre_valor)
        if match_nombre:
            # Coincidencia parcial: sugerir pero no asumir
            es_nuevo = False
            decision_nif = "warn"
            motivo_parts.append(
                f"NIF {nif_valor} no encontrado, pero nombre coincide con "
                f"NIF {match_nombre['nif']} en maestro — requiere verificación"
            )
        else:
            es_nuevo = True
            motivo_parts.append(f"Cliente nuevo: {nif_valor}")

    # La decisión final del campo es la peor entre identidad y maestro
    decision_final_nif = _peor_decision(decision_nif, nif_decision)
    decision_final_nombre = _peor_decision(decision_nombre, nombre_decision)

    motivo_nif = " | ".join(motivo_parts) if motivo_parts else f"NIF cliente extraído de {campo_nif} (libro={libro})"
    motivo_nombre = f"Nombre cliente extraído de {campo_nombre} (libro={libro})"

    # Decisión global = peor de los dos campos
    decision_global = _peor_decision(decision_final_nif, decision_final_nombre)

    return {
        "documento_id": documento_id,
        "fase": "4_cliente_destino",
        "version_politica": "v1",
        "timestamp": timestamp,
        "campos": {
            "nif_cliente": {
                "valor_final": nif_valor,
                "fuente_final": "identidad_cabecera",
                "confianza_final": nif_confianza,
                "decision": decision_final_nif,
                "motivo": motivo_nif,
                "candidatos": [],
                "validaciones": {"llm_usado": False},
            },
            "nombre_cliente": {
                "valor_final": nombre_valor,
                "fuente_final": "identidad_cabecera",
                "confianza_final": nombre_confianza,
                "decision": decision_final_nombre,
                "motivo": motivo_nombre,
                "candidatos": [],
                "validaciones": {"llm_usado": False},
            },
        },
        "cliente_info": {
            "es_nuevo": es_nuevo,
            "libro": libro,
            "campo_origen_nif": campo_nif,
            "campo_origen_nombre": campo_nombre,
        },
        "decision_global": decision_global,
    }


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

_PRIORIDAD = {"block": 3, "warn": 2, "pendiente": 1, "auto": 0}


def _peor_decision(a: str, b: str) -> str:
    """Devuelve la decisión con mayor prioridad (más grave)."""
    if _PRIORIDAD.get(a, 3) >= _PRIORIDAD.get(b, 3):
        return a
    return b


def _resultado_block(
    documento_id: str,
    timestamp: str,
    libro: str,
    motivo: str,
    campo_origen_nif: str = "",
    campo_origen_nombre: str = "",
) -> dict:
    """Genera un resultado completo con decisión block."""
    campo_block = {
        "valor_final": None,
        "fuente_final": "identidad_cabecera",
        "confianza_final": 0.0,
        "decision": "block",
        "motivo": motivo,
        "candidatos": [],
        "validaciones": {"llm_usado": False},
    }
    return {
        "documento_id": documento_id,
        "fase": "4_cliente_destino",
        "version_politica": "v1",
        "timestamp": timestamp,
        "campos": {
            "nif_cliente": {**campo_block},
            "nombre_cliente": {**campo_block},
        },
        "cliente_info": {
            "es_nuevo": False,
            "libro": libro,
            "campo_origen_nif": campo_origen_nif,
            "campo_origen_nombre": campo_origen_nombre,
        },
        "decision_global": "block",
    }
