"""
I/O del maestro de clientes.

Lee y escribe maestro_clientes.yaml. El maestro almacena
datos básicos de cada cliente identificado por NIF.

Schema v1 — espejo exacto del futuro CREATE TABLE clientes en Supabase
(la migración GCP será un INSERT ... SELECT directo):

    clientes:
      <nif>:                              # str — clave primaria
        nombre: str                       # razón social / nombre completo
        fecha_alta: str (ISO)             # primer procesado confirmado
        documentos_procesados: int        # total facturas auto+warn confirmadas
        ultima_factura_fecha: str|null    # max(fecha_expedicion) de auto+warn
        libros_activos: list[str]         # subset de {"compras","ventas","bienes"}
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import yaml

logger = logging.getLogger("pipeline.cliente_destino")

_MAESTRO_VACIO: dict = {"clientes": {}}

_DECISIONES_REGISTRABLES = {"auto", "warn"}


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


def registrar_cliente(
    maestro: dict,
    nif: str,
    nombre: str,
    fecha_expedicion: str | None = None,
    libro: str | None = None,
    decision: str = "auto",
) -> dict | None:
    """
    Registra/actualiza un cliente en el maestro (en memoria).

    Reglas (v1):
    - Si `decision` no es "auto" ni "warn", NO se registra y devuelve None.
    - Si el NIF no existe, crea entrada con schema v1 completo.
    - Incrementa `documentos_procesados`.
    - Si `fecha_expedicion` > `ultima_factura_fecha` actual, la actualiza.
    - Añade `libro` a `libros_activos` si no estaba.

    No escribe a disco — usar guardar_maestro() después.

    Returns:
        La entrada del cliente (nueva o actualizada), o None si decision no registra.
    """
    if decision not in _DECISIONES_REGISTRABLES:
        return None

    clientes = maestro.setdefault("clientes", {})

    if nif not in clientes:
        clientes[nif] = {
            "nombre": nombre,
            "fecha_alta": date.today().isoformat(),
            "documentos_procesados": 0,
            "ultima_factura_fecha": None,
            "libros_activos": [],
        }

    entry = clientes[nif]
    # Backfill defensivo: entradas escritas con schema antiguo pueden no tener
    # los campos nuevos. Los añadimos sin perder los existentes.
    entry.setdefault("ultima_factura_fecha", None)
    entry.setdefault("libros_activos", [])
    entry.setdefault("documentos_procesados", 0)

    entry["documentos_procesados"] = entry["documentos_procesados"] + 1

    if fecha_expedicion and (
        entry["ultima_factura_fecha"] is None
        or fecha_expedicion > entry["ultima_factura_fecha"]
    ):
        entry["ultima_factura_fecha"] = fecha_expedicion

    if libro and libro not in entry["libros_activos"]:
        entry["libros_activos"].append(libro)

    return entry


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
