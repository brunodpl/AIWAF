"""
Tests para el corrector determinista de tipo de IVA (hallazgo F2).

Cuando la columna "Tipo(%)" del documento es una letra (B/BH), Gemini a veces
toma el IMPORTE de IVA (23,50€) como PORCENTAJE (23,5%). Si el tipo extraído no
es un tipo legal pero cuota/base reconcilia con un tipo legal dentro de
tolerancia, el corrector lo sustituye y baja la confianza (revisión humana).
Es lógica pura y determinista: no infiere fuera de la aritmética cuota/base.
"""

from decimal import Decimal

from src.phase3_fiscal.tipo_corrector import corregir_tipo_iva_invalido

TIPOS_LEGALES = [0, 4, 10, 21]


def _linea(base, tipo, cuota, conf=0.95) -> dict:
    total = (base + cuota) if (base is not None and cuota is not None) else None
    return {
        "base_euros":      {"valor": base,  "confianza": conf},
        "tipo_porcentaje": {"valor": tipo,  "confianza": conf},
        "cuota":           {"valor": cuota, "confianza": conf},
        "total_linea":     {"valor": total, "confianza": conf},
    }


class TestCorregirTipoIvaInvalido:

    def test_corrige_importe_tomado_como_tipo(self):
        # 23,50 / 111,90 ≈ 21% → el "tipo" 23,5 era en realidad el importe del IVA.
        linea = _linea(111.90, 23.5, 23.50)
        r = corregir_tipo_iva_invalido(linea, TIPOS_LEGALES)
        assert r.corregido is True
        assert Decimal(str(r.tipo_corregido)) == Decimal("21")
        assert Decimal(str(linea["tipo_porcentaje"]["valor"])) == Decimal("21")
        # La corrección es heurística → confianza baja para forzar revisión.
        assert float(linea["tipo_porcentaje"]["confianza"]) <= 0.5

    def test_tipo_legal_no_cambia(self):
        linea = _linea(100.0, 21, 21.0)
        r = corregir_tipo_iva_invalido(linea, TIPOS_LEGALES)
        assert r.corregido is False
        assert linea["tipo_porcentaje"]["valor"] == 21
        assert linea["tipo_porcentaje"]["confianza"] == 0.95

    def test_tipo_no_legal_sin_reconciliacion_no_cambia(self):
        # base 100, cuota 23,5 → 23,5% no coincide con ningún tipo legal:
        # NO se toca (que FISCAL_007 bloquee).
        linea = _linea(100.0, 23.5, 23.5)
        r = corregir_tipo_iva_invalido(linea, TIPOS_LEGALES)
        assert r.corregido is False
        assert linea["tipo_porcentaje"]["valor"] == 23.5

    def test_linea_exenta_no_cambia(self):
        linea = _linea(100.0, None, None)
        r = corregir_tipo_iva_invalido(linea, TIPOS_LEGALES)
        assert r.corregido is False
        assert linea["tipo_porcentaje"]["valor"] is None

    def test_sin_base_no_puede_derivar(self):
        linea = _linea(None, 23.5, 23.50)
        r = corregir_tipo_iva_invalido(linea, TIPOS_LEGALES)
        assert r.corregido is False
