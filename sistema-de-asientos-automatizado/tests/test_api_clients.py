"""Tests de los endpoints /api/clients y /api/clients/{nif}/invoices."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient


def _seed_maestro(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    maestro = {"clientes": {e["nif"]: {k: v for k, v in e.items() if k != "nif"} for e in entries}}
    path.write_text(yaml.safe_dump(maestro, allow_unicode=True), encoding="utf-8")


def _seed_asiento(asientos_root: Path, doc_id: str, nif_cliente: str,
                  fecha: str, editado: bool = False,
                  origen_decision: str = "auto") -> Path:
    folder = asientos_root / f"compras_{doc_id}"
    folder.mkdir(parents=True)

    from src import state_writer
    state_writer.init(folder, doc_id, f"libros/facturas/compras/{doc_id}.pdf")

    (folder / "resultado_validacion.json").write_text(json.dumps({
        "decision_global": origen_decision,
        "campos": {"nif_cliente": {"valor_final": nif_cliente}},
    }), encoding="utf-8")

    (folder / "resultado_final.json").write_text(json.dumps({
        "schema_v": 1, "doc_id": doc_id, "libro": "compras",
        "confirmado_en": "2026-05-11T16:00:00+00:00",
        "origen_decision": origen_decision,
        "campos_finales": {
            "nif_cliente": {"valor": nif_cliente, "editado": False},
            "numero_factura": {"valor": f"F-{doc_id}", "editado": False},
            "fecha_expedicion": {"valor": fecha, "editado": False},
            "total_euros": {"valor": 100.0, "editado": False},
            "cuenta_contable": {"valor": "600000", "editado": editado},
        },
        "lineas_asiento": [],
        "csv_filename": f"asiento_{doc_id}.csv",
        "hash_csv": "sha256:abc",
    }), encoding="utf-8")
    return folder


@pytest.fixture
def api_client(tmp_path: Path):
    asientos_root = tmp_path / "asientos"
    asientos_root.mkdir(parents=True)
    maestro_path = tmp_path / "maestros" / "maestro_clientes.yaml"

    fake_cfg = SimpleNamespace(
        asientos_path=lambda: str(asientos_root),
        runtime_path=lambda: str(tmp_path / ".runtime"),
        maestro_clientes_path=str(maestro_path),
        output_path=str(tmp_path / "output"),
        logs_path=str(tmp_path / "logs"),
    )

    with patch("src.config.settings", return_value=fake_cfg), \
         patch("src.api.main.settings", return_value=fake_cfg):
        from src.api.main import app, _clients_cache, _invoices_cache
        _clients_cache.clear()
        _invoices_cache.clear()
        yield TestClient(app), asientos_root, maestro_path


def test_clients_yaml_vacio_devuelve_200_vacio(api_client):
    client, _, _ = api_client
    r = client.get("/api/clients")
    assert r.status_code == 200
    assert r.json() == {"clients": [], "total": 0}


def test_clients_devuelve_ordenado_por_ultima_factura_desc(api_client):
    client, _, maestro_path = api_client
    _seed_maestro(maestro_path, [
        {"nif": "B11111111", "nombre": "Antiguo SL",
         "fecha_alta": "2026-01-01", "documentos_procesados": 2,
         "ultima_factura_fecha": "2026-02-01",
         "libros_activos": ["compras"],
         "tipos_activos": ["cliente"]},
        {"nif": "B22222222", "nombre": "Reciente SL",
         "fecha_alta": "2026-04-01", "documentos_procesados": 5,
         "ultima_factura_fecha": "2026-04-28",
         "libros_activos": ["compras", "ventas"],
         "tipos_activos": ["cliente"]},
    ])
    r = client.get("/api/clients")
    body = r.json()
    assert body["total"] == 2
    assert body["clients"][0]["nif"] == "B22222222"
    assert body["clients"][1]["nif"] == "B11111111"


def test_clients_nif_invoices_solo_confirmadas_y_aislamiento(api_client):
    client, asientos_root, _ = api_client

    # 1 factura confirmada para B15234567
    _seed_asiento(asientos_root, "f001", "B15234567", "2026-04-21", editado=True)
    # 1 factura confirmada para otro NIF (no debe aparecer)
    _seed_asiento(asientos_root, "f002", "B99999999", "2026-04-22")
    # 1 carpeta sin resultado_final.json — no confirmada
    folder_no_final = asientos_root / "compras_f003"
    folder_no_final.mkdir()
    from src import state_writer
    state_writer.init(folder_no_final, "f003", "libros/facturas/compras/f003.pdf")

    r = client.get("/api/clients/B15234567/invoices")
    body = r.json()
    assert body["nif"] == "B15234567"
    docs = [i["doc_id"] for i in body["invoices"]]
    assert docs == ["f001"]
    assert body["invoices"][0]["tiene_ediciones"] is True


def test_clients_nif_invoices_incluye_block_aprobado(api_client):
    """Un block aprobado por el operario (resultado_final.json existe) aparece
    en el historial — el filtro por origen_decision se eliminó porque el
    fichero solo se crea tras confirmación humana."""
    client, asientos_root, _ = api_client
    _seed_asiento(asientos_root, "f001", "B15234567", "2026-04-21",
                  origen_decision="block")
    r = client.get("/api/clients/B15234567/invoices")
    docs = [i["doc_id"] for i in r.json()["invoices"]]
    assert docs == ["f001"]


def test_clients_solo_devuelve_clientes_gestoria(api_client):
    """GET /api/clients filtra a entradas con tipos_activos=['cliente'].
    Proveedores y entradas legacy sin tipos_activos se omiten."""
    client, _, maestro_path = api_client
    _seed_maestro(maestro_path, [
        {"nif": "B11111111", "nombre": "Cliente SL",
         "fecha_alta": "2026-01-01", "documentos_procesados": 1,
         "ultima_factura_fecha": "2026-02-01",
         "libros_activos": ["compras"], "tipos_activos": ["cliente"]},
        {"nif": "B22222222", "nombre": "Prov SL",
         "fecha_alta": "2026-01-01", "documentos_procesados": 1,
         "ultima_factura_fecha": "2026-02-01",
         "libros_activos": ["compras"], "tipos_activos": ["proveedor"]},
        {"nif": "B33333333", "nombre": "Legacy SL",
         "fecha_alta": "2026-01-01", "documentos_procesados": 1,
         "ultima_factura_fecha": "2026-02-01",
         "libros_activos": ["compras"]},
    ])

    body = client.get("/api/clients").json()
    nifs = [e["nif"] for e in body["clients"]]
    assert nifs == ["B11111111"]
    assert body["total"] == 1
    assert body["clients"][0]["tipos_activos"] == ["cliente"]


def test_invoices_by_client_includes_lineas_asiento(api_client):
    """GET /api/clients/{nif}/invoices must return lineas_asiento per invoice."""
    client, asientos_root, _ = api_client

    nif = "B12345678"
    doc_id = "f_lineas_001"
    folder = _seed_asiento(asientos_root, doc_id, nif, "2026-01-15")

    lineas = [
        {"cuenta": "600", "concepto": "Compras", "debe": 100.0, "haber": 0.0},
        {"cuenta": "472", "concepto": "IVA soportado", "debe": 21.0, "haber": 0.0},
        {"cuenta": "400", "concepto": "Proveedor", "debe": 0.0, "haber": 121.0},
    ]
    final_data = json.loads((folder / "resultado_final.json").read_text(encoding="utf-8"))
    final_data["lineas_asiento"] = lineas
    (folder / "resultado_final.json").write_text(json.dumps(final_data), encoding="utf-8")

    resp = client.get(f"/api/clients/{nif}/invoices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nif"] == nif
    assert len(body["invoices"]) == 1
    inv = body["invoices"][0]
    assert "lineas_asiento" in inv, "lineas_asiento missing from response"
    assert len(inv["lineas_asiento"]) == 3
    assert inv["lineas_asiento"][0]["cuenta"] == "600"
