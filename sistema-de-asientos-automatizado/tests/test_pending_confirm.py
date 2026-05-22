"""Tests de la guardia .pending_confirm.json y su expansión en /api/pipeline/status."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _fake_cfg(tmp_path: Path):
    return SimpleNamespace(
        asientos_path=lambda: str(tmp_path / "asientos"),
        runtime_path=lambda: str(tmp_path / ".runtime"),
        maestro_clientes_path=str(tmp_path / "maestros" / "maestro_clientes.yaml"),
        # inbox_path(libro_short) → <tmp_path>/facturas/<libro_short>
        # Necesario para el fallback de limpieza de PDF en el confirm endpoint.
        inbox_path=lambda libro: str(tmp_path / "facturas" / libro),
        libros_base=str(tmp_path),
        output_path=str(tmp_path / "output"),
        logs_path=str(tmp_path / "logs"),
    )


@pytest.fixture
def client(tmp_path: Path):
    (tmp_path / ".runtime").mkdir(parents=True)
    (tmp_path / "asientos").mkdir(parents=True)
    cfg = _fake_cfg(tmp_path)
    with patch("src.config.settings", return_value=cfg), \
         patch("src.api.main.settings", return_value=cfg):
        from src.api.main import app
        yield TestClient(app), tmp_path


def test_pipeline_status_sin_pending_confirm(client):
    c, _ = client
    r = c.get("/api/pipeline/status")
    assert r.status_code == 200
    body = r.json()
    assert body["pending_confirm"] is False
    assert body["pending_doc_ids"] == []


def test_pipeline_status_con_pending_confirm(client):
    c, tmp_path = client
    pending = tmp_path / ".runtime" / ".pending_confirm.json"
    pending.write_text(json.dumps({
        "status": "running",
        "started_at": "2026-05-11T16:00:00Z",
        "doc_ids_lote": ["factura_001", "factura_002"],
    }), encoding="utf-8")

    r = c.get("/api/pipeline/status")
    body = r.json()
    assert body["pending_confirm"] is True
    assert body["pending_doc_ids"] == ["factura_001", "factura_002"]


def test_confirm_borra_pending_confirm(client):
    """Tras confirmar el lote, el flag pending_confirm debe quedar limpio."""
    c, tmp_path = client
    pending = tmp_path / ".runtime" / ".pending_confirm.json"
    pending.write_text(json.dumps({
        "status": "running",
        "started_at": "2026-05-11T16:00:00Z",
        "doc_ids_lote": ["factura_001"],
    }), encoding="utf-8")

    # Setup asiento mínimo confirmable
    asientos = tmp_path / "asientos"
    folder = asientos / "compras_factura_001"
    folder.mkdir(parents=True)
    (folder / "resultado_validacion.json").write_text(json.dumps({
        "decision_global": "auto",
        "campos": {"nif_cliente": {"valor_final": "B11"}},
    }), encoding="utf-8")
    from src import state_writer
    state_writer.init(folder, "factura_001", "x.pdf")

    payload = {
        "doc_ids": ["factura_001"],
        "asientos": {
            "factura_001": {
                "campos_finales": {
                    "nif_cliente": {"valor": "B11"},
                    "fecha_expedicion": {"valor": "2026-04-01"},
                },
                "lineas_asiento": [],
                "csv_b64": base64.b64encode(b"x").decode("ascii"),
            }
        },
    }
    r = c.post("/api/pipeline/confirm", json=payload)
    assert r.status_code == 200, r.text
    assert not pending.exists()
