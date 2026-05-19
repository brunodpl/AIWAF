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
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pipeline.state")

SCHEMA_V = 1
SIDECAR_NAME = ".state.json"

# Locks por carpeta para serializar lecturas-modificaciones-escrituras del
# sidecar dentro del mismo proceso. Evita la race "leer→leer→escribir A→
# escribir B" cuando la API (action endpoint) y el pipeline background corren
# en threads distintos sobre el mismo documento.
#
# El lock NO protege frente a otros procesos — para eso bastaría un lockfile,
# pero el sistema está diseñado como single-process (un único contenedor
# pipeline-api). Si en el futuro se escala horizontalmente, sustituir por
# `fcntl`/`msvcrt.locking` sobre el propio sidecar.
_locks_global = threading.Lock()
_locks_per_folder: dict[str, threading.Lock] = {}


def _lock_for(folder: Path) -> threading.Lock:
    key = str(Path(folder).resolve())
    with _locks_global:
        lock = _locks_per_folder.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks_per_folder[key] = lock
        return lock

# Valores válidos de ``status`` en cada evento.
#
# Ciclo de vida de un documento:
#
#   uploaded ─► processing ─► (done|review|blocked|error)
#                  │             │       │
#                  │ retrying    │       │
#                  ▼             ▼       │
#               (...)        confirmed   │
#                                        │
#   cancelled ◄── pipeline.cancel mid-doc en cualquier punto de processing
#   renamed   ── tras cierre exitoso, evento intermedio
#   reset     ── evento dejado en sidecar copia antes de borrar (forense)
#
# Único terminal contable = ``confirmed``. ``done`` es "esperando confirm humano".
#
# ``split``: el splitter pre-OCR (Fase 1) marca con este estado el sidecar del
# PDF original cuando lo divide en N facturas individuales. Es terminal — el
# original ya no será procesado; la trazabilidad continúa en los doc_ids de
# ``split_into``. Permite que ``is_terminal()`` skipee la carpeta en runs
# posteriores y a la vez conservar la cadena de custodia (RGPD).
VALID_STATUSES = frozenset(
    {
        "uploaded",
        "processing",
        "retrying",
        "review",
        "done",
        "confirmed",
        "blocked",
        "cancelled",
        "error",
        "renamed",
        "reset",
        "split",
    }
)

# Transiciones aceptadas. Si una transición no está en la tabla, ``append()``
# emite un WARNING al log técnico pero no lanza — preferimos resiliencia a
# romper el sidecar de un documento. Si en producción aparecen muchos warnings
# de una transición concreta, revisar la tabla.
VALID_TRANSITIONS: dict[Optional[str], frozenset[str]] = {
    None:         frozenset({"uploaded", "processing"}),
    "uploaded":   frozenset({"processing", "cancelled", "error", "split"}),
    "processing": frozenset({"done", "review", "blocked", "error", "cancelled", "retrying", "renamed", "split"}),
    "retrying":   frozenset({"processing", "done", "review", "blocked", "error", "cancelled"}),
    "review":     frozenset({"done", "review", "blocked", "renamed", "cancelled"}),
    "done":       frozenset({"confirmed", "review", "renamed"}),
    "confirmed":  frozenset({"renamed"}),
    "blocked":    frozenset({"retrying", "renamed"}),
    "cancelled":  frozenset({"retrying"}),
    "error":      frozenset({"retrying", "processing"}),
    "renamed":    frozenset({"done", "review", "blocked", "confirmed", "error", "cancelled", "retrying"}),
    "reset":      frozenset(),
    "split":      frozenset(),
}


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

    Compat: este punto de entrada se mantiene para callers legacy (CLI
    standalone, tests). El flujo nuevo via UI/API debe usar
    ``init_uploaded`` en el endpoint de upload y luego registrar
    ``processing`` con ``append`` cuando el pipeline lo recoja.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with _lock_for(folder):
        sidecar = _sidecar_path(folder)
        if sidecar.exists():
            # Reproceso: aprovechamos `_append_locked` para no soltar el lock.
            _append_locked(folder, {
                "status": "processing", "file_origin": file_origin, "folder": folder.name,
            })
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


def init_uploaded(
    folder: Path,
    doc_id: str,
    file_origin: str,
    sha256_file: str,
    size_bytes: int,
) -> None:
    """Crea el sidecar con primer evento ``uploaded`` (flujo via UI/API).

    Llama al `state_writer` desde `POST /api/upload_files` tras confirmar
    fsync + visibilidad del fichero. NO es idempotente: si el sidecar ya
    existe, lanza ``FileExistsError`` — el caller debe haber comprobado
    duplicados antes (via `find_by_file_sha256`).
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with _lock_for(folder):
        sidecar = _sidecar_path(folder)
        if sidecar.exists():
            raise FileExistsError(
                f"Sidecar ya existe en {folder}; el upload no debe sobreescribir."
            )

        payload = {
            "schema_v": SCHEMA_V,
            "doc_id": doc_id,
            "events": [
                {
                    "ts": _now_iso(),
                    "status": "uploaded",
                    "file_origin": file_origin,
                    "folder": folder.name,
                    "sha256_file": sha256_file,
                    "size_bytes": size_bytes,
                }
            ],
        }
        _atomic_write(sidecar, payload)
        logger.info(
            "[state] init_uploaded doc_id=%s folder=%s size=%d",
            doc_id, folder.name, size_bytes,
        )


def append(folder: Path, event: dict[str, Any]) -> None:
    """Añade un evento al final de la lista. Nunca sobreescribe entradas previas.

    Serializado por ``_lock_for(folder)`` para evitar carreras cuando la API
    (acción humana) y el pipeline background tocan el mismo asiento.
    """
    folder = Path(folder)
    with _lock_for(folder):
        _append_locked(folder, event)


def _append_locked(folder: Path, event: dict[str, Any]) -> None:
    """Implementación de append que asume lock ya tomado."""
    sidecar = _sidecar_path(folder)
    if not sidecar.exists():
        raise FileNotFoundError(f"Sidecar no existe en {folder}; llama a init() primero.")

    status = event.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"status inválido: {status!r} (válidos: {sorted(VALID_STATUSES)})")

    data = read(folder)
    events = data.get("events") or []
    prev_status: Optional[str] = events[-1].get("status") if events else None

    # Validación de transición: WARN, no lanza. Preferimos resiliencia a
    # romper el sidecar de un documento si la realidad operativa diverge
    # ligeramente de la tabla.
    allowed = VALID_TRANSITIONS.get(prev_status)
    if allowed is not None and status not in allowed:
        logger.warning(
            "[state] transición inusual doc_id=%s %s -> %s (permitidos: %s)",
            data.get("doc_id"), prev_status, status, sorted(allowed),
        )

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


def is_done(folder: Path) -> bool:
    """True si el último evento del sidecar es ``status=done``.

    Nota: ``done`` significa "lista para confirmar", NO terminal contable.
    Para terminal contable usa ``is_confirmed`` o ``is_terminal``.
    """
    return current_status(folder) == "done"


def is_pending_confirm(folder: Path) -> bool:
    """True si el último status del sidecar es ``done`` (listo para /confirm)."""
    return current_status(folder) == "done"


def is_terminal(folder: Path) -> bool:
    """True si la factura ya no se puede mover de estado por sí sola.

    Estados terminales: ``confirmed``, ``blocked``, ``cancelled``, ``error``,
    ``split``. NOTA: ``done`` NO es terminal — está esperando confirm humano.
    """
    return current_status(folder) in {"confirmed", "blocked", "cancelled", "error", "split"}


def is_split(folder: Path) -> bool:
    """True si el sidecar refleja que el PDF fue dividido por el splitter Fase 1.

    Carpeta inerte para el pipeline: el original vive en `_originales/` y la
    trazabilidad continúa en los doc_ids listados en ``split_into`` del evento.
    """
    return current_status(folder) == "split"


def is_confirmed(folder: Path) -> bool:
    """True si la factura está confirmada (asiento contable cerrado).

    Acepta dos formatos:

    - **Nuevo** (recomendado): último status del sidecar == ``confirmed``.
    - **Legacy**: existe un evento con ``status="done", action="confirmed"``
      (sidecars escritos antes del refactor de lifecycle). Se conserva
      esta lectura para idempotencia del batch confirm sobre instalaciones
      ya en producción.
    """
    folder = Path(folder)
    if not _sidecar_path(folder).exists():
        return False
    if current_status(folder) == "confirmed":
        return True
    data = read(folder)
    for ev in data.get("events") or []:
        if ev.get("status") == "done" and ev.get("action") == "confirmed":
            return True
    return False


def rename(folder: Path, new_name: str) -> Path:
    """Renombra la carpeta a ``new_name``, resolviendo colisiones con sufijo ``_2``, ``_3``...

    Emite un evento ``renamed`` en el sidecar tras el rename. Si hubo colisión,
    el evento incluye ``collision=true`` y el ``to`` final con sufijo.

    Serializado bajo el lock de la carpeta de origen para no chocar con
    `append()` concurrentes durante el rename.

    Devuelve el ``Path`` resultante.
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Carpeta origen no existe: {folder}")

    with _lock_for(folder):
        parent = folder.parent
        target = parent / new_name
        collision = False
        suffix = 2
        while target.exists() and target.resolve() != folder.resolve():
            target = parent / f"{new_name}_{suffix}"
            suffix += 1
            collision = True

        if target.resolve() == folder.resolve():
            return folder

        os.replace(folder, target)
        # Tras renombrar, el lock por la carpeta nueva apunta a un keypath
        # distinto. Llamamos a `_append_locked` pasando la nueva carpeta;
        # como el lock viejo ya cubre la transición, no hay race aquí.
        _append_locked(
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


def find_by_file_sha256(asientos_root: Path, sha256_hex: str) -> Optional[Path]:
    """Busca la carpeta cuyo evento ``uploaded`` tenga ``sha256_file==sha256_hex``.

    Útil para detectar duplicados físicos en el endpoint de upload antes
    de escribir nada en disco. Devuelve None si no hay match.
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
            for ev in data.get("events") or []:
                if ev.get("sha256_file") == sha256_hex:
                    return child
        except (OSError, json.JSONDecodeError):
            continue
    return None


def find_by_fiscal_hash(asientos_root: Path, fiscal_hash: str) -> Optional[Path]:
    """Busca la carpeta cuyo sidecar tenga un evento con ``fiscal_hash==fiscal_hash``.

    El ``fiscal_hash`` se computa tras Fase 3 (identidad cabecera) como
    sha256_16(nif_emisor + numero_factura + fecha_expedicion) y se anota
    en el evento de cierre del pipeline. Permite detectar duplicados
    fiscales aunque el PDF sea distinto.
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
            for ev in data.get("events") or []:
                if ev.get("fiscal_hash") == fiscal_hash:
                    return child
        except (OSError, json.JSONDecodeError):
            continue
    return None
