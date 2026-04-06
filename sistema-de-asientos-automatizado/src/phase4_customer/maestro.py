"""
I/O del maestro de clientes.

Lee y escribe maestro_clientes.yaml. El maestro almacena
datos básicos de cada cliente identificado por NIF.

Schema:
    clientes:
      B12345674:
        nombre: "PROVEEDOR EJEMPLO SL"
        fecha_alta: "2026-04-04"
        documentos_procesados: 1
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import yaml

logger = logging.getLogger("pipeline.cliente_destino")

_MAESTRO_VACIO: dict = {"clientes": {}}


def cargar_maestro(path: str) -> dict:
    """
    Carga maestro de clientes desde YAML.

    Devuelve {"clientes": {}} si el fichero está vacío, no existe o es inválido.
    """
    p = Path(path)
    if not p.exists():
        logger.info(f"[maestro] Fichero no encontrado, se usará maestro vacío: {path}")
        return {**_MAESTRO_VACIO}

    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning(f"[maestro] Error leyendo maestro, se usará vacío: {e}")
        return {**_MAESTRO_VACIO}

    if data is None or not isinstance(data, dict):
        return {**_MAESTRO_VACIO}

    if "clientes" not in data:
        data["clientes"] = {}

    return data


def buscar_cliente(maestro: dict, nif: str) -> dict | None:
    """
    Busca un cliente por NIF en el maestro.

    Returns:
        dict con datos del cliente si existe, None si no.
    """
    clientes = maestro.get("clientes", {})
    entry = clientes.get(nif)
    if entry is None:
        return None
    return {"nif": nif, **entry}


def registrar_cliente(maestro: dict, nif: str, nombre: str) -> dict:
    """
    Registra un nuevo cliente en el maestro (en memoria).

    Si el NIF ya existe, incrementa documentos_procesados.
    No escribe a disco — usar guardar_maestro() después.

    Returns:
        La entrada del cliente (nueva o actualizada).
    """
    clientes = maestro.setdefault("clientes", {})

    if nif in clientes:
        clientes[nif]["documentos_procesados"] = clientes[nif].get("documentos_procesados", 0) + 1
    else:
        clientes[nif] = {
            "nombre": nombre,
            "fecha_alta": date.today().isoformat(),
            "documentos_procesados": 1,
        }

    return clientes[nif]


def guardar_maestro(path: str, maestro: dict) -> None:
    """Escribe el maestro de clientes a disco en formato YAML."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(
            maestro,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    logger.info(f"[maestro] Maestro guardado: {len(maestro.get('clientes', {}))} clientes en {path}")
