"""Regression tests para Bug 3 — progress bar multi-libro.

`run_pipeline` se invoca una vez por cada libro con archivos pendientes. Antes
del fix, `_update_status_file` escribía `processed=summary[ok+warn+error]` de
ese libro, sobrescribiendo el contador acumulado. Resultado: barra de progreso
regresaba al pasar de un libro al siguiente.

Fix: parámetro `processed_offset` en `run_pipeline` que se suma al contador
local antes de persistirlo.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.pipeline import _update_status_file


def _seed_status(path: Path, processed: int, total: int) -> None:
    path.write_text(json.dumps({"processed": processed, "total": total}), encoding="utf-8")


def _read_status(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_update_status_file_writes_absolute_processed(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _seed_status(status, processed=0, total=5)

    _update_status_file(str(status), processed=3)

    assert _read_status(status)["processed"] == 3


def test_processed_offset_accumulates_between_books(tmp_path: Path) -> None:
    """Simula la orquestación de api/main.py: tras procesar libro1 (3 archivos)
    y libro2 (2 archivos), processed debe llegar a 5, no quedarse en 2."""
    status = tmp_path / "status.json"
    _seed_status(status, processed=0, total=5)

    libro1_offset = 0
    libro1_local_processed = 3
    _update_status_file(
        str(status), processed=libro1_offset + libro1_local_processed
    )
    assert _read_status(status)["processed"] == 3

    libro2_offset = 3
    for local in range(1, 3):
        _update_status_file(
            str(status), processed=libro2_offset + local
        )
    assert _read_status(status)["processed"] == 5


def test_total_field_preserved_across_updates(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _seed_status(status, processed=0, total=10)

    _update_status_file(str(status), processed=4)
    _update_status_file(str(status), processed=8)

    data = _read_status(status)
    assert data["total"] == 10
    assert data["processed"] == 8


def test_run_pipeline_accepts_processed_offset_kwarg() -> None:
    """Smoke: la firma de run_pipeline acepta processed_offset (signature contract)."""
    import inspect

    from src.pipeline import run_pipeline

    sig = inspect.signature(run_pipeline)
    assert "processed_offset" in sig.parameters
    assert sig.parameters["processed_offset"].default == 0
