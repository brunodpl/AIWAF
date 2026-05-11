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
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

if sys.platform == "win32":
    import msvcrt

    def _acquire_lock(fh) -> None:
        # LK_LOCK bloquea hasta liberar; reintenta cada segundo internamente.
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _release_lock(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _release_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

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
        return {"clientes": {}}

    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning(f"[maestro] Error leyendo maestro, se usará vacío: {e}")
        return {"clientes": {}}

    if data is None or not isinstance(data, dict):
        return {"clientes": {}}

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


def _merge_cliente(disk: dict, mem: dict) -> dict:
    """
    Fusiona dos entradas del mismo NIF. Reglas deterministas:
    - documentos_procesados = max(disk, mem)
    - ultima_factura_fecha  = max(disk, mem) (None < cualquier fecha)
    - libros_activos        = unión preservando orden disk-primero
    - nombre, fecha_alta    = del disco si existe, si no del mem
    """
    out = {**disk}
    out["nombre"] = disk.get("nombre") or mem.get("nombre", "")
    out["fecha_alta"] = disk.get("fecha_alta") or mem.get("fecha_alta")
    out["documentos_procesados"] = max(
        disk.get("documentos_procesados", 0),
        mem.get("documentos_procesados", 0),
    )
    fechas = [d for d in (disk.get("ultima_factura_fecha"), mem.get("ultima_factura_fecha")) if d]
    out["ultima_factura_fecha"] = max(fechas) if fechas else None

    libros_disk = list(disk.get("libros_activos", []) or [])
    for lib in mem.get("libros_activos", []) or []:
        if lib not in libros_disk:
            libros_disk.append(lib)
    out["libros_activos"] = libros_disk
    return out


def _merge_maestros(disk: dict, mem: dict) -> dict:
    """Fusiona dos maestros completos NIF a NIF."""
    out_clientes: dict[str, dict] = {}
    disk_clientes = disk.get("clientes", {}) or {}
    mem_clientes = mem.get("clientes", {}) or {}
    nifs = set(disk_clientes) | set(mem_clientes)
    for nif in nifs:
        if nif in disk_clientes and nif in mem_clientes:
            out_clientes[nif] = _merge_cliente(disk_clientes[nif], mem_clientes[nif])
        elif nif in disk_clientes:
            out_clientes[nif] = disk_clientes[nif]
        else:
            out_clientes[nif] = mem_clientes[nif]
    return {"clientes": out_clientes}


def guardar_maestro(path: str, maestro: dict) -> None:
    """
    Escribe el maestro a disco bajo file-lock cross-process.

    Patrón: abre `{path}.lock` → lock exclusivo → relee disco → fusiona con
    `maestro` en memoria → tmp + os.replace atómico → libera lock.

    Garantiza que escrituras concurrentes (intra-proceso o entre procesos en
    Cloud Run con volumen compartido) no pierdan NIFs ni decrementen contadores.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(p) + ".lock"

    with open(lock_path, "a+", encoding="utf-8") as lf:
        _acquire_lock(lf)
        try:
            disk_actual = cargar_maestro(str(p))
            merged = _merge_maestros(disk_actual, maestro)

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=p.name + ".",
                suffix=".tmp",
                dir=str(p.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    yaml.dump(
                        merged,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                os.replace(tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        finally:
            _release_lock(lf)

    logger.info(
        f"[maestro] Maestro guardado: {len(maestro.get('clientes', {}))} clientes en {path}"
    )
