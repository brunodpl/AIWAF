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
    client, asientos_root, maestro_path = api_client
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
    # Nuevo contrato: solo aparecen clientes con asiento real bajo libros/asientos.
    _seed_asiento(asientos_root, "f1", "B11111111", "2026-02-01")
    _seed_asiento(asientos_root, "f2", "B22222222", "2026-04-28")
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
    Proveedores y entradas legacy sin tipos_activos se omiten. Adicionalmente
    el NIF debe aparecer en al menos un resultado_final.json bajo
    libros/asientos/ — no basta con estar en el maestro."""
    client, asientos_root, maestro_path = api_client
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
    # Sembramos asientos reales para los 3 NIFs. Aún así solo aparece el
    # cliente (B11111111) — el filtro tipos_activos="cliente" debe seguir
    # funcionando aunque el NIF tenga asiento.
    _seed_asiento(asientos_root, "f1", "B11111111", "2026-02-01")
    _seed_asiento(asientos_root, "f2", "B22222222", "2026-02-01")
    _seed_asiento(asientos_root, "f3", "B33333333", "2026-02-01")

    body = client.get("/api/clients").json()
    nifs = [e["nif"] for e in body["clients"]]
    assert nifs == ["B11111111"]
    assert body["total"] == 1
    assert body["clients"][0]["tipos_activos"] == ["cliente"]


def test_clients_prefiere_nombre_de_resultado_final_si_maestro_vacio(api_client):
    """Si el maestro tiene nombre="" para un NIF (porque la primera confirmación
    se hizo con nombre_entidad vacío), el historial debe mostrar el nombre que
    aparece en el resultado_final.json más reciente — la verdad fiscal del
    documento, no el maestro stale.

    Regresión: /api/clients devolvía nombre="" porque solo leía maestro.
    """
    client, asientos_root, maestro_path = api_client
    _seed_maestro(maestro_path, [
        {"nif": "49915950Q", "nombre": "",
         "fecha_alta": "2026-05-19", "documentos_procesados": 1,
         "ultima_factura_fecha": "2026-05-19",
         "libros_activos": ["ventas"], "tipos_activos": ["cliente"]},
    ])
    # Seed asiento de ventas con nombre_cliente bien rellenado.
    folder = asientos_root / "ventas_20265000763-N0208"
    folder.mkdir(parents=True)
    from src import state_writer
    state_writer.init(folder, "20265000763-N0208",
                      "libros/facturas/ventas/20265000763-N0208.pdf")
    (folder / "resultado_final.json").write_text(json.dumps({
        "schema_v": 1, "doc_id": "20265000763-N0208", "libro": "ventas",
        "confirmado_en": "2026-05-19T08:42:24+00:00",
        "origen_decision": "auto",
        "campos_finales": {
            "nif_cliente": {"valor": "49915950Q", "editado": False},
            "nombre_cliente": {"valor": "DAVILA PEREZ RECHE", "editado": True},
            "fecha_expedicion": {"valor": "2026-05-19", "editado": False},
        },
        "lineas_asiento": [],
        "csv_filename": "asiento_20265000763-N0208.csv",
        "hash_csv": "sha256:abc",
    }), encoding="utf-8")

    body = client.get("/api/clients").json()
    assert body["total"] == 1
    assert body["clients"][0]["nif"] == "49915950Q"
    assert body["clients"][0]["nombre"] == "DAVILA PEREZ RECHE"


def test_clients_usa_nombre_resultado_final_mas_reciente(api_client):
    """Cuando hay varios resultado_final.json para el mismo NIF, el historial
    debe quedarse con el nombre de la factura con fecha de expedición más
    reciente (a igualdad de fechas, el último no-vacío encontrado)."""
    client, asientos_root, maestro_path = api_client
    _seed_maestro(maestro_path, [
        {"nif": "49915950Q", "nombre": "",
         "fecha_alta": "2026-05-19", "documentos_procesados": 2,
         "ultima_factura_fecha": "2026-05-19",
         "libros_activos": ["ventas"], "tipos_activos": ["cliente"]},
    ])

    def _seed_venta(doc_id: str, fecha: str, nombre: str) -> None:
        folder = asientos_root / f"ventas_{doc_id}"
        folder.mkdir(parents=True)
        from src import state_writer
        state_writer.init(folder, doc_id, f"libros/facturas/ventas/{doc_id}.pdf")
        (folder / "resultado_final.json").write_text(json.dumps({
            "schema_v": 1, "doc_id": doc_id, "libro": "ventas",
            "confirmado_en": "2026-05-19T08:42:24+00:00",
            "origen_decision": "auto",
            "campos_finales": {
                "nif_cliente": {"valor": "49915950Q", "editado": False},
                "nombre_cliente": {"valor": nombre, "editado": False},
                "fecha_expedicion": {"valor": fecha, "editado": False},
            },
            "lineas_asiento": [],
            "csv_filename": f"asiento_{doc_id}.csv",
            "hash_csv": "sha256:abc",
        }), encoding="utf-8")

    _seed_venta("N0208", "2026-04-01", "DAVILA NOMBRE VIEJO")
    _seed_venta("N0209", "2026-05-19", "DAVILA PEREZ RECHE")

    body = client.get("/api/clients").json()
    assert body["clients"][0]["nombre"] == "DAVILA PEREZ RECHE"


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
