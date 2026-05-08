"""Sidecar de estado por documento (.state.json).

Patrón append-only. Cada documento procesado tiene una carpeta en
``libros/asientos/{folder_name}/`` con un fichero ``.state.json`` que
registra la línea de vida del documento como una lista de eventos.

El orquestador (``pipeline.py``) y los endpoints de la API son los
únicos llamadores. Las fases NO escriben aquí — devuelven resultados
al orquestador, que es quien anota el evento correspondiente.

El "estado actual" de un documento es el ``status`` del último evento.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pipeline.state")

SCHEMA_V = 1
SIDECAR_NAME = ".state.json"

# Valores válidos de ``status`` en cada evento.
VALID_STATUSES = frozenset(
    {"processing", "review", "done", "blocked", "error", "renamed", "reset"}
)


def _now_iso() -> str:
    """ISO-8601 UTC con sufijo ``Z`` (segundos)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sidecar_path(folder: Path) -> Path:
    return folder / SIDECAR_NAME


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Escritura atómica: tmp en mismo dir + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        # Limpieza best-effort si algo falla antes del replace.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def init(folder: Path, doc_id: str, file_origin: str) -> None:
    """Crea el sidecar inicial con el primer evento ``processing``.

    Idempotente: si el sidecar ya existe, se respeta y no se sobreescribe
    (escenario de reproceso). En ese caso simplemente añade un evento
    nuevo de ``processing`` para señalar el reinicio.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    sidecar = _sidecar_path(folder)

    if sidecar.exists():
        append(folder, {"status": "processing", "file_origin": file_origin, "folder": folder.name})
        return

    payload = {
        "schema_v": SCHEMA_V,
        "doc_id": doc_id,
        "events": [
            {
                "ts": _now_iso(),
                "status": "processing",
                "file_origin": file_origin,
                "folder": folder.name,
            }
        ],
    }
    _atomic_write(sidecar, payload)
    logger.info("[state] init doc_id=%s folder=%s", doc_id, folder.name)


def append(folder: Path, event: dict[str, Any]) -> None:
    """Añade un evento al final de la lista. Nunca sobreescribe entradas previas."""
    folder = Path(folder)
    sidecar = _sidecar_path(folder)
    if not sidecar.exists():
        raise FileNotFoundError(f"Sidecar no existe en {folder}; llama a init() primero.")

    status = event.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r} (válidos: {sorted(VALID_STATUSES)})")

    data = read(folder)
    full_event = {"ts": _now_iso(), **event}
    data["events"].append(full_event)
    _atomic_write(sidecar, data)
    logger.info("[state] append doc_id=%s status=%s", data.get("doc_id"), status)


def read(folder: Path) -> dict[str, Any]:
    """Lee el sidecar. Lanza si no existe."""
    folder = Path(folder)
    sidecar = _sidecar_path(folder)
    with sidecar.open("r", encoding="utf-8") as f:
        return json.load(f)


def current_status(folder: Path) -> Optional[str]:
    """Devuelve el ``status`` del último evento, o None si el sidecar no existe."""
    folder = Path(folder)
    if not _sidecar_path(folder).exists():
        return None
    data = read(folder)
    events = data.get("events") or []
    return events[-1].get("status") if events else None


def rename(folder: Path, new_name: str) -> Path:
    """Renombra la carpeta a ``new_name``, resolviendo colisiones con sufijo ``_2``, ``_3``...

    Emite un evento ``renamed`` en el sidecar tras el rename. Si hubo colisión,
    el evento incluye ``collision=true`` y el ``to`` final con sufijo.

    Devuelve el ``Path`` resultante.
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Carpeta origen no existe: {folder}")

    parent = folder.parent
    target = parent / new_name
    collision = False
    suffix = 2
    while target.exists() and target.resolve() != folder.resolve():
        target = parent / f"{new_name}_{suffix}"
        suffix += 1
        collision = True

    if target.resolve() == folder.resolve():
        # ya tiene el nombre deseado; no-op
        return folder

    os.replace(folder, target)
    append(
        target,
        {
            "status": "renamed",
            "from": folder.name,
            "to": target.name,
            **({"collision": True} if collision else {}),
        },
    )
    logger.info(
        "[state] rename %s -> %s collision=%s", folder.name, target.name, collision
    )
    return target


def find_folder_by_doc_id(asientos_root: Path, doc_id: str) -> Optional[Path]:
    """Busca la carpeta que pertenece a ``doc_id`` escaneando ``asientos_root``.

    Lee la cabecera del sidecar de cada carpeta. Devuelve None si no se encuentra.
    """
    asientos_root = Path(asientos_root)
    if not asientos_root.exists():
        return None
    for child in asientos_root.iterdir():
        if not child.is_dir():
            continue
        sidecar = _sidecar_path(child)
        if not sidecar.exists():
            continue
        try:
            with sidecar.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("doc_id") == doc_id:
                return child
        except (OSError, json.JSONDecodeError):
            continue
    return None
