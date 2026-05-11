"""Tests del módulo final_writer (resultado_final.json + CSV físico + hash)."""

from __future__ import annotations

import hashlib
import json

from src.final_writer import write_final


def _validacion_warn() -> dict:
    return {
        "decision_global": "warn",
        "campos": {
            "nif_entidad": {"valor_final": "B15234567"},
            "cuenta_contable": {"valor_final": "600001"},
            "fecha_expedicion": {"valor_final": "2026-04-21"},
            "nif_cliente": {"valor_final": "B27891234"},
        },
    }


def test_write_final_marca_editado_solo_donde_difiere(tmp_path):
    (tmp_path / "resultado_validacion.json").write_text(
        json.dumps(_validacion_warn()), encoding="utf-8"
    )
    campos_user = {
        "nif_entidad": {"valor": "B15234567"},
        "cuenta_contable": {"valor": "600000"},  # difiere → editado=True
        "fecha_expedicion": {"valor": "2026-04-21"},
        "nif_cliente": {"valor": "B27891234"},
    }
    csv_bytes = b"cuenta;debe;haber\n600000;100;0\n"

    write_final(
        str(tmp_path),
        doc_id="factura_001",
        libro="compras",
        campos_finales=campos_user,
        lineas=[{"cuenta": "600000", "debe": 100, "haber": 0}],
        csv_bytes=csv_bytes,
    )

    final = json.loads((tmp_path / "resultado_final.json").read_text(encoding="utf-8"))
    assert final["schema_v"] == 1
    assert final["doc_id"] == "factura_001"
    assert final["libro"] == "compras"
    assert final["origen_decision"] == "warn"
    assert final["campos_finales"]["nif_entidad"]["editado"] is False
    assert final["campos_finales"]["cuenta_contable"]["editado"] is True
    assert final["campos_finales"]["fecha_expedicion"]["editado"] is False

    csv_path = tmp_path / "asiento_factura_001.csv"
    assert csv_path.exists()
    expected = "sha256:" + hashlib.sha256(csv_bytes).hexdigest()
    assert final["hash_csv"] == expected
    assert final["csv_filename"] == "asiento_factura_001.csv"


def test_write_final_sin_resultado_validacion_no_marca_editados(tmp_path):
    """Si no existe validación previa (caso defensivo) ningún campo se marca como editado."""
    campos_user = {"nif_entidad": {"valor": "B15234567"}}
    write_final(
        str(tmp_path),
        doc_id="factura_002",
        libro="ventas",
        campos_finales=campos_user,
        lineas=[],
        csv_bytes=b"",
    )
    final = json.loads((tmp_path / "resultado_final.json").read_text(encoding="utf-8"))
    assert final["campos_finales"]["nif_entidad"]["editado"] is False
    assert final["origen_decision"] is None
