"""
Integración del corrector de tipo IVA en el orquestador de fase 3 fiscal (F2).

Verifica que, con la corrección aplicada antes de validar, la factura M/24/1
(tipo 23,5 leído por error como porcentaje) pasa de BLOCK a WARN: el tipo se
deriva a 21% (cuota 23,50 reconcilia con base 111,90) y queda en revisión humana.
"""

import json
import tempfile
from pathlib import Path

from src.phase3_fiscal.main import run_fiscal


def _doc_extraido(base, tipo, cuota, total) -> dict:
    return {
        "fiscal": {
            "total_euros": {"valor": total, "confianza": 0.95},
            "requiere_revision": False,
            "lineas_fiscales": [
                {
                    "base_euros":      {"valor": base,  "confianza": 0.95},
                    "tipo_porcentaje": {"valor": tipo,  "confianza": 0.95},
                    "cuota":           {"valor": cuota, "confianza": 0.95},
                    "total_linea":     {"valor": base + cuota, "confianza": 0.95},
                }
            ],
        }
    }


def test_corrige_tipo_y_pasa_de_block_a_warn():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "documento_extraido.json").write_text(
            json.dumps(_doc_extraido(111.90, 23.5, 23.50, 135.40)), encoding="utf-8"
        )

        ok = run_fiscal("M-24-1", tmp)
        assert ok

        res = json.loads((Path(tmp) / "resultado_fiscal.json").read_text(encoding="utf-8"))
        # Sin corrección sería block (138,20 vs 135,40); con corrección → warn.
        assert res["decision_global"] == "warn"
        assert res["requiere_revision_humana"] is True

        linea = res["campos"]["lineas_fiscales"]["valor_final"][0]
        assert linea["tipo_porcentaje"] == "21,00"  # _format_importe → coma decimal


def test_tipo_no_reconciliable_sigue_en_block():
    # Línea aritméticamente inconsistente: tipo 23,5 no legal y cuota 50,00 no
    # deriva a ningún tipo legal (44,7%). El corrector no toca → FISCAL_004/007
    # fallan → block (sin downgrade a warn).
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "documento_extraido.json").write_text(
            json.dumps(_doc_extraido(111.90, 23.5, 50.00, 200.00)), encoding="utf-8"
        )

        ok = run_fiscal("M-24-X", tmp)
        assert ok

        res = json.loads((Path(tmp) / "resultado_fiscal.json").read_text(encoding="utf-8"))
        assert res["decision_global"] == "block"
