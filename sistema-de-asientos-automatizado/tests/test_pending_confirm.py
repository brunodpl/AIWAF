"""Tests de la guardia .pending_confirm.json y su expansión en /api/pipeline/status."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.pipeline import _write_pending_confirm


class _CfgRT:
    def __init__(self, runtime): self._r = runtime
    def runtime_path(self): return self._r


def test_merge_acumula_doc_ids_de_varios_libros(tmp_path):
    cfg = _CfgRT(str(tmp_path))
    _write_pending_confirm(cfg, ["compras_a"], merge=True)
    _write_pending_confirm(cfg, ["ventas_b"], merge=True)
    _write_pending_confirm(cfg, ["bienes_c", "compras_a"], merge=True)  # dup ignored
    data = json.loads((tmp_path / ".pending_confirm.json").read_text(encoding="utf-8"))
    assert data["doc_ids_lote"] == ["compras_a", "ventas_b", "bienes_c"]


def test_sin_merge_sobrescribe(tmp_path):
    cfg = _CfgRT(str(tmp_path))
    _write_pending_confirm(cfg, ["a"])
    _write_pending_confirm(cfg, ["b"])
    data = json.loads((tmp_path / ".pending_confirm.json").read_text(encoding="utf-8"))
    assert data["doc_ids_lote"] == ["b"]


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


def test_confirm_parcial_preserva_resto_del_lote(client):
    """Confirm de un subconjunto NO debe borrar pending_confirm — los doc_ids
    no confirmados (porque el pipeline aún los está procesando, o porque el
    operario los dejará para una siguiente tanda) tienen que seguir visibles
    para ``/api/pipeline/batch`` y por extensión para el filtro batchDocIds
    del reviewer. Regresión del bug "EMPEZAR A REVISAR (N FACTURAS LISTAS)"
    que aterrizaba en "No hay facturas en proceso" tras un confirm parcial.
    """
    c, tmp_path = client
    pending = tmp_path / ".runtime" / ".pending_confirm.json"
    pending.write_text(json.dumps({
        "status": "running",
        "started_at": "2026-05-11T16:00:00Z",
        "doc_ids_lote": ["factura_001", "factura_002", "factura_003"],
    }), encoding="utf-8")

    # Solo preparamos sidecar+validation para la 001 — las otras dos siguen
    # "en vuelo" (sin folder), como si el pipeline aún no las hubiera tocado.
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

    # Fichero sigue ahí, con las dos restantes (orden preservado).
    assert pending.exists(), "pending_confirm no debe borrarse con confirm parcial"
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["doc_ids_lote"] == ["factura_002", "factura_003"]

    # /api/pipeline/status sigue reportando el lote pendiente con esos doc_ids.
    body = c.get("/api/pipeline/status").json()
    assert body["pending_confirm"] is True
    assert body["pending_doc_ids"] == ["factura_002", "factura_003"]


def test_confirm_parcial_idempotente(client):
    """Re-confirmar los mismos doc_ids no debe corromper el lote (red flaky,
    retry del frontend, etc.). La 2ª llamada debe ser no-op sobre el fichero.
    """
    c, tmp_path = client
    pending = tmp_path / ".runtime" / ".pending_confirm.json"
    pending.write_text(json.dumps({
        "status": "running",
        "started_at": "2026-05-11T16:00:00Z",
        "doc_ids_lote": ["factura_001", "factura_002"],
    }), encoding="utf-8")

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
    # 1ª llamada.
    assert c.post("/api/pipeline/confirm", json=payload).status_code == 200
    # 2ª llamada (retry).
    assert c.post("/api/pipeline/confirm", json=payload).status_code == 200

    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["doc_ids_lote"] == ["factura_002"]
