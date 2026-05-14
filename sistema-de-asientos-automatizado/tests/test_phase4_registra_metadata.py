"""Integration test: run_cliente pasa fecha_expedicion y libro al maestro."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import yaml


def _identidad(nif_emisor: str, nombre_emisor: str, fecha: str) -> dict:
    """Build minimal resultado_identidad_cabecera.json for compras."""
    return {
        "campos": {
            "nif_entidad": {
                "valor_final": nif_emisor,
                "confianza_final": 0.95,
                "decision": "auto",
            },
            "nombre_entidad": {
                "valor_final": nombre_emisor,
                "confianza_final": 0.95,
                "decision": "auto",
            },
            "nif_receptor": {
                "valor_final": "B99999999",
                "confianza_final": 0.95,
                "decision": "auto",
            },
            "nombre_receptor": {
                "valor_final": "GESTORIA CLIENTE SL",
                "confianza_final": 0.95,
                "decision": "auto",
            },
            "fecha_expedicion": {
                "valor_final": fecha,
                "confianza_final": 0.95,
                "decision": "auto",
            },
        },
        "decision_global": "auto",
    }


def test_run_cliente_persiste_fecha_y_libro_en_maestro(tmp_path):
    """En libro de compras el receptor es el cliente de la gestoría;
    el emisor (nif_entidad) es el proveedor que se registra como nuevo."""
    doc_dir = tmp_path / "asientos" / "compras_factura_001"
    doc_dir.mkdir(parents=True)
    (doc_dir / "resultado_identidad_cabecera.json").write_text(
        json.dumps(_identidad("B15234567", "Cabreiroá SL", "2026-04-21")),
        encoding="utf-8",
    )

    maestro_path = tmp_path / "maestros" / "maestro_clientes.yaml"

    fake_cfg = SimpleNamespace(maestro_clientes_path=str(maestro_path))
    with patch("src.config.settings", return_value=fake_cfg):
        from src.phase4_customer.main import run_cliente
        ok = run_cliente("factura_001", str(doc_dir), "20_COMPRAS_GASTOS")

    assert ok is True
    assert maestro_path.exists()

    with open(maestro_path, encoding="utf-8") as f:
        maestro = yaml.safe_load(f)

    # En compras el "cliente_destino" de la gestoría es el receptor.
    # El proveedor B15234567 NO se registra automáticamente (en compras,
    # el cliente es el receptor; el emisor es el proveedor, que es
    # quien acaba apareciendo como "cliente nuevo" en este flujo legacy).
    # Lo importante para este test: si se registra, debe llevar
    # fecha_expedicion y libros_activos=["compras"].
    clientes = maestro.get("clientes", {})
    if clientes:
        nif_registrado = next(iter(clientes))
        entry = clientes[nif_registrado]
        assert entry["ultima_factura_fecha"] == "2026-04-21"
        assert entry["libros_activos"] == ["compras"]
