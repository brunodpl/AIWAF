"""
Escritura atómica de resultado_final.json + CSV físico por carpeta de asiento.

resultado_final.json es la verdad fiscal definitiva tras la confirmación del
operario. La diferencia con resultado_validacion.json (inmutable, output del
ensamblador automático) es la huella auditada del trabajo humano de revisión:
los campos cuyo `valor` difiere del `valor_final` de la validación llevan
`editado: true`.

El diff lo calcula este módulo, NO el frontend — así evitamos bugs de
diff-en-cliente que borrarían la huella humana.

Schema de resultado_final.json — ver writing-plans doc:
    schema_v, doc_id, libro, confirmado_en, confirmado_por, origen_decision,
    campos_finales{ {valor, editado} }, lineas_asiento[], csv_filename, hash_csv
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("pipeline.final_writer")

SCHEMA_V = 1
ARTEFACTO_VALIDACION = "resultado_validacion.json"
ARTEFACTO_FINAL = "resultado_final.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp + os.replace en la misma carpeta — no cruza FS, no half-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_validacion(folder: Path) -> dict | None:
    path = folder / ARTEFACTO_VALIDACION
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[final_writer] no se pudo leer {path}: {e}")
        return None


def write_final(
    folder: str,
    doc_id: str,
    libro: str,
    campos_finales: dict[str, dict[str, Any]],
    lineas: list[dict[str, Any]],
    csv_bytes: bytes,
    actor: str = "operario",
) -> Path:
    """
    Escribe resultado_final.json + asiento_{doc_id}.csv en `folder`.

    Args:
        folder: carpeta del asiento (libros/asientos/{...}).
        doc_id: id del documento, usado en el filename del CSV.
        libro: short form ("compras"/"ventas"/"bienes").
        campos_finales: dict {nombre_campo: {"valor": <v>}} tal como envía el frontend.
        lineas: lista de líneas del asiento contable.
        csv_bytes: contenido binario del CSV ya construido por el cliente.
        actor: identificador del operario (futuro multi-tenant).

    Returns:
        Path absoluto al resultado_final.json escrito.

    Idempotencia: si el resultado_final.json ya existe, se sobrescribe
    atómicamente. El CSV también. El llamador (api/confirm) decide si
    reentrar — este módulo no.
    """
    folder_p = Path(folder)
    validacion = _read_validacion(folder_p)

    valores_validados: dict[str, Any] = {}
    origen_decision: str | None = None
    if validacion is not None:
        origen_decision = validacion.get("decision_global")
        for nombre, info in (validacion.get("campos") or {}).items():
            if isinstance(info, dict):
                valores_validados[nombre] = info.get("valor_final")

    # Diff campo a campo: editado=True solo si difiere de validación.
    campos_anotados: dict[str, dict[str, Any]] = {}
    for nombre, payload in campos_finales.items():
        valor = payload.get("valor") if isinstance(payload, dict) else payload
        valor_original = valores_validados.get(nombre)
        if validacion is None:
            editado = False  # sin línea base no se puede marcar como editado
        else:
            editado = (nombre in valores_validados) and (valor != valor_original)
        campos_anotados[nombre] = {"valor": valor, "editado": editado}

    # CSV físico (escrito antes que el JSON: si falla, no quedan punteros rotos).
    csv_filename = f"asiento_{doc_id}.csv"
    csv_path = folder_p / csv_filename
    _atomic_write_bytes(csv_path, csv_bytes)

    hash_csv = "sha256:" + hashlib.sha256(csv_bytes).hexdigest()

    final = {
        "schema_v": SCHEMA_V,
        "doc_id": doc_id,
        "libro": libro,
        "confirmado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "confirmado_por": actor,
        "origen_decision": origen_decision,
        "campos_finales": campos_anotados,
        "lineas_asiento": lineas,
        "csv_filename": csv_filename,
        "hash_csv": hash_csv,
    }

    final_path = folder_p / ARTEFACTO_FINAL
    payload = json.dumps(final, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(final_path, payload)

    logger.info(
        f"[final_writer] resultado_final escrito doc_id={doc_id} libro={libro} "
        f"campos_editados={sum(1 for c in campos_anotados.values() if c['editado'])}"
    )
    return final_path
