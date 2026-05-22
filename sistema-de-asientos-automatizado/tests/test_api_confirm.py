"""Tests del endpoint POST /api/pipeline/confirm."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def libros_root(tmp_path: Path) -> Path:
    """
    Crea un libros/ con una carpeta de asiento ya en estado review (warn) y
    resultado_validacion.json listo para que confirm lo procese.
    """
    asientos = tmp_path / "asientos"
    folder = asientos / "compras_factura_001"
    folder.mkdir(parents=True)

    # resultado_validacion.json — output inmutable del ensamblador
    (folder / "resultado_validacion.json").write_text(json.dumps({
        "decision_global": "warn",
        "campos": {
            "nif_entidad": {"valor_final": "B15234567"},
            "nombre_entidad": {"valor_final": "Cabreiroá SL"},
            "cuenta_contable": {"valor_final": "600001"},
            "fecha_expedicion": {"valor_final": "2026-04-21"},
            "nif_cliente": {"valor_final": "B99999999"},
            "nombre_cliente": {"valor_final": "GESTORÍA"},
        },
    }), encoding="utf-8")

    # .state.json — sidecar con un primer evento processing + review
    from src import state_writer
    state_writer.init(folder, "factura_001", "libros/facturas/compras/factura_001.pdf")
    state_writer.append(folder, {"status": "review", "decision": "warn"})

    return tmp_path


@pytest.fixture
def api_client(libros_root: Path, tmp_path: Path):
    """
    Construye un TestClient con settings() parcheado para apuntar al árbol
    temporal del test.
    """
    maestro_path = tmp_path / "maestros" / "maestro_clientes.yaml"
    runtime_path = libros_root / ".runtime"
    runtime_path.mkdir(parents=True, exist_ok=True)

    fake_cfg = SimpleNamespace(
        asientos_path=lambda: str(libros_root / "asientos"),
        runtime_path=lambda: str(runtime_path),
        maestro_clientes_path=str(maestro_path),
        # inbox_path(libro_short) → <libros_root>/facturas/<libro_short>
        # Necesario para el fallback de limpieza de PDF en el confirm endpoint.
        inbox_path=lambda libro: str(libros_root / "facturas" / libro),
        libros_base=str(libros_root),
        # Campos opcionales que el módulo api/main.py pueda tocar
        output_path=str(tmp_path / "output"),
        logs_path=str(tmp_path / "logs"),
    )

    with patch("src.config.settings", return_value=fake_cfg), \
         patch("src.api.main.settings", return_value=fake_cfg):
        from src.api.main import app
        yield TestClient(app), libros_root, maestro_path


def _payload_factura_001(cuenta_editada: str = "600000") -> dict:
    csv = "cuenta;debe;haber\n600000;100;0\n".encode("utf-8")
    return {
        "doc_ids": ["factura_001"],
        "asientos": {
            "factura_001": {
                "campos_finales": {
                    "nif_entidad": {"valor": "B15234567"},
                    "nombre_entidad": {"valor": "Cabreiroá SL"},
                    "cuenta_contable": {"valor": cuenta_editada},
                    "fecha_expedicion": {"valor": "2026-04-21"},
                    "nif_cliente": {"valor": "B99999999"},
                    "nombre_cliente": {"valor": "GESTORÍA"},
                },
                "lineas_asiento": [
                    {"cuenta": "600000", "concepto": "X", "debe": 100, "haber": 0}
                ],
                "csv_b64": base64.b64encode(csv).decode("ascii"),
            }
        },
    }


def test_confirm_escribe_resultado_final_y_csv(api_client):
    client, libros_root, maestro_path = api_client
    r = client.post("/api/pipeline/confirm", json=_payload_factura_001())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["facturas_confirmadas"] == 1

    folder = libros_root / "asientos" / "compras_factura_001"
    final = json.loads((folder / "resultado_final.json").read_text(encoding="utf-8"))
    assert final["origen_decision"] == "warn"
    assert final["campos_finales"]["cuenta_contable"]["editado"] is True
    assert final["campos_finales"]["nif_entidad"]["editado"] is False
    assert (folder / "asiento_factura_001.csv").exists()

    # state.json última event = confirmed (el endpoint usa status="confirmed" desde el refactor)
    state = json.loads((folder / ".state.json").read_text(encoding="utf-8"))
    assert state["events"][-1]["status"] == "confirmed"

    # maestro: cliente registrado
    maestro = yaml.safe_load(maestro_path.read_text(encoding="utf-8"))
    assert "B99999999" in maestro["clientes"]
    assert maestro["clientes"]["B99999999"]["ultima_factura_fecha"] == "2026-04-21"
    assert maestro["clientes"]["B99999999"]["libros_activos"] == ["compras"]


def test_confirm_es_idempotente(api_client):
    """Dos llamadas con el mismo payload no duplican el evento done."""
    client, libros_root, _ = api_client
    payload = _payload_factura_001()
    r1 = client.post("/api/pipeline/confirm", json=payload)
    r2 = client.post("/api/pipeline/confirm", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200

    folder = libros_root / "asientos" / "compras_factura_001"
    state = json.loads((folder / ".state.json").read_text(encoding="utf-8"))
    # El endpoint escribe status="confirmed" (no "done") desde el refactor de lifecycle.
    confirmed_events = [e for e in state["events"] if e.get("status") == "confirmed"]
    assert len(confirmed_events) == 1, f"Esperaba 1 evento confirmed, encontré {len(confirmed_events)}"


def test_confirm_doc_id_inexistente_devuelve_404(api_client):
    """
    Un doc_id inexistente no levanta 404 — el endpoint devuelve 200 con
    ok=False y el error en la lista errors (diseño batch: un fallo parcial
    no aborta el lote completo). La aserción original era stale.
    """
    client, _, _ = api_client
    payload = {
        "doc_ids": ["factura_inexistente"],
        "asientos": {
            "factura_inexistente": {
                "campos_finales": {},
                "lineas_asiento": [],
                "csv_b64": "",
            }
        },
    }
    r = client.post("/api/pipeline/confirm", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert any(e["doc_id"] == "factura_inexistente" for e in body["errors"])


def test_confirm_pendiente_registra_cliente(api_client, libros_root):
    """
    Con decision_global='pendiente' (semántica sin resolver), el confirm
    del operario debe registrar igualmente el cliente en el maestro.

    Antes del fix, registrar_cliente() filtraba 'pendiente' silenciosamente
    y el cliente nunca aparecía en /historial.
    """
    # Reescribir resultado_validacion con decision_global=pendiente
    folder = libros_root / "asientos" / "compras_factura_001"
    validation = json.loads((folder / "resultado_validacion.json").read_text(encoding="utf-8"))
    validation["decision_global"] = "pendiente"
    (folder / "resultado_validacion.json").write_text(
        json.dumps(validation), encoding="utf-8"
    )

    client, _, maestro_path = api_client
    r = client.post("/api/pipeline/confirm", json=_payload_factura_001())
    assert r.status_code == 200, r.text

    maestro = yaml.safe_load(maestro_path.read_text(encoding="utf-8"))
    # B99999999 es el nif_cliente del payload — debe estar registrado
    assert "B99999999" in maestro["clientes"], (
        "Factura con decision_global='pendiente' confirmada por humano: "
        "cliente debe quedar en el maestro"
    )


def test_confirm_no_doble_conteo_nif_igual(api_client):
    """When nif_entidad == nif_receptor the entity must be counted only once."""
    client, libros_root, maestro_path = api_client

    nif = "B12345678"
    doc_id = "factura_doble_nif"
    folder = libros_root / "asientos" / f"compras_{doc_id}"
    folder.mkdir(parents=True)

    from src import state_writer as sw
    sw.init(folder, doc_id, f"libros/facturas/compras/{doc_id}.pdf")
    sw.append(folder, {"status": "review", "decision": "warn"})

    (folder / "resultado_validacion.json").write_text(json.dumps({
        "decision_global": "warn",
        "campos": {
            "nif_entidad":     {"valor_final": nif},
            "nombre_entidad":  {"valor_final": "Prov SL"},
            "nif_receptor":    {"valor_final": nif},
            "nombre_receptor": {"valor_final": "Prov SL"},
            "nif_cliente":     {"valor_final": nif},
            "nombre_cliente":  {"valor_final": "Prov SL"},
            "fecha_expedicion": {"valor_final": "2026-01-15"},
        },
    }), encoding="utf-8")

    payload = {
        "doc_ids": [doc_id],
        "asientos": {
            doc_id: {
                "campos_finales": {
                    "nif_entidad":     {"valor": nif},
                    "nombre_entidad":  {"valor": "Prov SL"},
                    "nif_receptor":    {"valor": nif},
                    "nombre_cliente":  {"valor": "Prov SL"},
                    "nif_cliente":     {"valor": nif},
                    "fecha_expedicion": {"valor": "2026-01-15"},
                    "cuenta_contable":  {"valor": "600000"},
                },
                "lineas_asiento": [],
                "csv_b64": base64.b64encode(b"cuenta;debe;haber\n600000;100;0\n").decode(),
            }
        },
    }

    resp = client.post("/api/pipeline/confirm", json=payload)
    assert resp.status_code == 200, resp.text

    maestro_path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(maestro_path.read_text(encoding="utf-8"))
    assert nif in data.get("clientes", {}), "NIF not registered at all"
    count = data["clientes"][nif]["documentos_procesados"]
    assert count == 1, f"Expected 1 but got {count} — duplicate NIF was counted twice"
