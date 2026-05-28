"""
Integration test: run_cliente NO modifica el maestro durante el escaneo.

Contrato nuevo (TDD):
  - run_cliente escribe resultado_cliente.json con los campos resueltos.
  - run_cliente NO crea ni modifica maestro_clientes.yaml; el maestro es
    responsabilidad exclusiva del endpoint de confirmación.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch



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


def test_run_cliente_no_escribe_maestro_durante_scan(tmp_path):
    """run_cliente resuelve el cliente pero NO toca el maestro.

    La escritura del maestro es responsabilidad exclusiva del confirm.
    Después del scan:
      - resultado_cliente.json debe existir con campos del cliente resuelto.
      - maestro_clientes.yaml NO debe haber sido creado.
    """
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

    # (a) La función debe terminar sin error
    assert ok is True

    # (b) resultado_cliente.json debe existir con campos resueltos
    output_path = doc_dir / "resultado_cliente.json"
    assert output_path.exists(), "resultado_cliente.json debe escribirse durante el scan"
    with open(output_path, encoding="utf-8") as f:
        resultado = json.load(f)
    assert "cliente_info" in resultado, "resultado_cliente.json debe incluir cliente_info"
    assert "decision_global" in resultado, "resultado_cliente.json debe incluir decision_global"

    # (c) maestro_clientes.yaml NO debe haber sido creado por el scan
    assert not maestro_path.exists(), (
        "El scan NO debe crear ni modificar maestro_clientes.yaml — "
        "eso es responsabilidad exclusiva del endpoint de confirmación"
    )
