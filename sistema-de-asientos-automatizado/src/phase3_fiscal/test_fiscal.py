"""
Tests para la fase 3.2: Verificación fiscal determinista.

Sin I/O — pasa dicts directamente a verificador.py.
Estilo replicado de test_3_cabecera.py.
"""

from __future__ import annotations

from decimal import Decimal

from src.phase3_fiscal.verificador import (
    verificar_fiscal,
    safe_decimal,
    _clasificar_linea,
    ClasificacionLinea,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _campo(valor, confianza=0.95):
    """Construye un campo con estructura {valor, confianza}."""
    return {"valor": valor, "confianza": confianza}


def _linea(base, tipo, cuota, total_linea=None, confianza=0.95):
    """Construye una línea fiscal con la estructura del mapper."""
    linea = {
        "base_euros": _campo(base, confianza),
        "tipo_porcentaje": _campo(tipo, confianza),
        "cuota": _campo(cuota, confianza),
    }
    if total_linea is not None:
        linea["total_linea"] = _campo(total_linea, confianza)
    else:
        linea["total_linea"] = _campo(None, 0.0)
    return linea


def _fiscal(total, lineas, requiere_revision=False):
    """Construye la sección fiscal completa."""
    return {
        "total_euros": _campo(total, 0.95),
        "requiere_revision": requiere_revision,
        "lineas_fiscales": lineas,
    }


def _get_regla(resultado, codigo, donde="bloques_fiscales", indice_bloque=0):
    """Extrae una regla por código del resultado."""
    if donde == "bloques_fiscales":
        bloque = resultado["bloques_fiscales"][indice_bloque]
        for r in bloque["reglas"]:
            if r["codigo"] == codigo:
                return r
    elif donde == "reglas_factura":
        for r in resultado["reglas_factura"]:
            if r["codigo"] == codigo:
                return r
    return None


# ── Test 1: Un tramo 21%, todo correcto → auto ──────────────────────────────

class TestUnTramo21Auto:
    def test_decision_global_auto(self):
        fiscal = _fiscal(121.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "auto"
        assert resultado["campos"]["total_euros"]["decision"] == "auto"
        assert resultado["campos"]["lineas_fiscales"]["decision"] == "auto"
        assert resultado["requiere_revision_humana"] is False

    def test_reglas_linea_ok(self):
        fiscal = _fiscal(121.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        resultado = verificar_fiscal(fiscal)

        bloque = resultado["bloques_fiscales"][0]
        assert bloque["clasificacion"] == "gravada"
        for regla in bloque["reglas"]:
            assert regla["estado"] == "ok", f"{regla['codigo']} no es ok"


# ── Test 2: Dos tramos 21% + 10%, todo correcto → auto ──────────────────────

class TestDosTramos21y10Auto:
    def test_decision_global_auto(self):
        lineas = [
            _linea(100.00, 21.0, 21.00, 121.00),
            _linea(200.00, 10.0, 20.00, 220.00),
        ]
        fiscal = _fiscal(341.00, lineas)
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "auto"
        assert resultado["clasificacion_lineas"]["gravadas"] == 2

    def test_fiscal_008_multitramo(self):
        lineas = [
            _linea(100.00, 21.0, 21.00, 121.00),
            _linea(200.00, 10.0, 20.00, 220.00),
        ]
        fiscal = _fiscal(341.00, lineas)
        resultado = verificar_fiscal(fiscal)

        regla_008 = _get_regla(resultado, "FISCAL_008", "reglas_factura")
        assert regla_008["estado"] == "ok"


# ── Test 3: Cuota incorrecta, diff > 0.02€ → block ──────────────────────────

class TestCuotaIncorrectaBlock:
    def test_decision_block(self):
        # cuota debería ser 21.00, se pone 21.50 (diff=0.50)
        fiscal = _fiscal(121.50, [_linea(100.00, 21.0, 21.50, 121.50)])
        resultado = verificar_fiscal(fiscal)

        assert resultado["campos"]["lineas_fiscales"]["decision"] == "block"
        regla_004 = _get_regla(resultado, "FISCAL_004")
        assert regla_004["estado"] == "fail"


# ── Test 4: Cuota incorrecta, diff ≤ 0.02€ → auto (tolerancia) ──────────────

class TestCuotaDentroToleranciaAuto:
    def test_decision_auto(self):
        # cuota debería ser 21.00, se pone 21.01 (diff=0.01)
        fiscal = _fiscal(121.01, [_linea(100.00, 21.0, 21.01, 121.01)])
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "auto"
        regla_004 = _get_regla(resultado, "FISCAL_004")
        assert regla_004["estado"] == "ok"


# ── Test 5: Total factura no cuadra con suma líneas → block ──────────────────

class TestTotalFacturaNoCuadra:
    def test_decision_block(self):
        # Línea suma 121.00 pero total dice 130.00
        fiscal = _fiscal(130.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        resultado = verificar_fiscal(fiscal)

        assert resultado["campos"]["total_euros"]["decision"] == "block"
        regla_007 = _get_regla(resultado, "FISCAL_007", "reglas_factura")
        assert regla_007["estado"] == "fail"


# ── Test 6: lineas_fiscales vacío → pendiente ───────────────────────────────

class TestSinLineasPendiente:
    def test_decision_pendiente(self):
        fiscal = _fiscal(121.00, [])
        resultado = verificar_fiscal(fiscal)

        assert resultado["campos"]["lineas_fiscales"]["decision"] == "pendiente"
        regla_006 = _get_regla(resultado, "FISCAL_006", "reglas_factura")
        assert regla_006["estado"] == "fail"


# ── Test 7: total_euros None → pendiente ─────────────────────────────────────

class TestTotalEurosNonePendiente:
    def test_decision_pendiente(self):
        fiscal = {
            "total_euros": {"valor": None, "confianza": 0.0},
            "requiere_revision": False,
            "lineas_fiscales": [_linea(100.00, 21.0, 21.00, 121.00)],
        }
        resultado = verificar_fiscal(fiscal)

        assert resultado["campos"]["total_euros"]["decision"] == "pendiente"
        assert resultado["decision_global"] in ("pendiente", "block")


# ── Test 8: base_euros no parseable ("N/A") → FISCAL_001 error, block ────────

class TestBaseNoParseable:
    def test_decision_block(self):
        linea = {
            "base_euros": _campo("N/A"),
            "tipo_porcentaje": _campo(21.0),
            "cuota": _campo(21.00),
            "total_linea": _campo(121.00),
        }
        fiscal = _fiscal(121.00, [linea])
        resultado = verificar_fiscal(fiscal)

        regla_001 = _get_regla(resultado, "FISCAL_001")
        assert regla_001["estado"] == "error"
        assert resultado["campos"]["lineas_fiscales"]["decision"] == "block"


# ── Test 9: total_linea ausente → FISCAL_005 no_ejecutada, resto ok ──────────

class TestTotalLineaAusente:
    def test_fiscal_005_no_ejecutada(self):
        # Sin total_linea
        linea = {
            "base_euros": _campo(100.00),
            "tipo_porcentaje": _campo(21.0),
            "cuota": _campo(21.00),
            "total_linea": _campo(None),
        }
        fiscal = _fiscal(121.00, [linea])
        resultado = verificar_fiscal(fiscal)

        regla_005 = _get_regla(resultado, "FISCAL_005")
        assert regla_005["estado"] == "no_ejecutada"
        # Resto de reglas ok
        regla_004 = _get_regla(resultado, "FISCAL_004")
        assert regla_004["estado"] == "ok"
        assert resultado["decision_global"] == "auto"


# ── Test 10: requiere_revision=True del OCR → warning propagado ─────────────

class TestRequiereRevisionOCR:
    def test_warning_propagado(self):
        fiscal = _fiscal(121.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        fiscal["requiere_revision"] = True
        resultado = verificar_fiscal(fiscal)

        assert resultado["requiere_revision_ocr"] is True
        assert any("requiere_revision" in w for w in resultado["warnings"])


# ── Test 11: Línea tipo null (exenta) → warn ────────────────────────────────

class TestLineaExentaWarn:
    def test_decision_warn(self):
        # Línea exenta: tipo=null, cuota=0
        linea = {
            "base_euros": _campo(100.00),
            "tipo_porcentaje": _campo(None),
            "cuota": _campo(0.00),
            "total_linea": _campo(100.00),
        }
        fiscal = _fiscal(100.00, [linea])
        resultado = verificar_fiscal(fiscal)

        assert resultado["campos"]["lineas_fiscales"]["decision"] == "warn"
        assert resultado["clasificacion_lineas"]["exentas"] == 1
        assert any("exenta" in w for w in resultado["warnings"])

    def test_no_es_block(self):
        linea = {
            "base_euros": _campo(100.00),
            "tipo_porcentaje": _campo(None),
            "cuota": _campo(0.00),
            "total_linea": _campo(100.00),
        }
        fiscal = _fiscal(100.00, [linea])
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] != "block"


# ── Test 12: Línea tipo 0.0 → auto si cuadra ────────────────────────────────

class TestLineaTipoCeroAuto:
    def test_decision_auto(self):
        # Tipo 0%: cuota=0, total=base
        linea = {
            "base_euros": _campo(500.00),
            "tipo_porcentaje": _campo(0.0),
            "cuota": _campo(0.00),
            "total_linea": _campo(500.00),
        }
        fiscal = _fiscal(500.00, [linea])
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "auto"
        assert resultado["clasificacion_lineas"]["tipo_cero"] == 1
        bloque = resultado["bloques_fiscales"][0]
        assert bloque["clasificacion"] == "tipo_cero"


# ── Test 13: Gravada 21% + exenta → decision_global warn ────────────────────

class TestMezclaGravadaExenta:
    def test_decision_global_warn(self):
        lineas = [
            _linea(100.00, 21.0, 21.00, 121.00),
            {
                "base_euros": _campo(50.00),
                "tipo_porcentaje": _campo(None),
                "cuota": _campo(0.00),
                "total_linea": _campo(50.00),
            },
        ]
        fiscal = _fiscal(171.00, lineas)
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "warn"
        assert resultado["clasificacion_lineas"]["gravadas"] == 1
        assert resultado["clasificacion_lineas"]["exentas"] == 1


# ── Test 14: Gravada 21% + tipo 0% → decision_global auto ───────────────────

class TestMezclaGravadaTipoCero:
    def test_decision_global_auto(self):
        lineas = [
            _linea(100.00, 21.0, 21.00, 121.00),
            {
                "base_euros": _campo(200.00),
                "tipo_porcentaje": _campo(0.0),
                "cuota": _campo(0.00),
                "total_linea": _campo(200.00),
            },
        ]
        fiscal = _fiscal(321.00, lineas)
        resultado = verificar_fiscal(fiscal)

        assert resultado["decision_global"] == "auto"
        assert resultado["clasificacion_lineas"]["gravadas"] == 1
        assert resultado["clasificacion_lineas"]["tipo_cero"] == 1


# ── Tests adicionales de utilidades ──────────────────────────────────────────

class TestSafeDecimal:
    def test_float(self):
        assert safe_decimal(21.0) == Decimal("21.0")

    def test_int(self):
        assert safe_decimal(100) == Decimal("100")

    def test_string(self):
        assert safe_decimal("0.02") == Decimal("0.02")

    def test_none(self):
        assert safe_decimal(None) is None

    def test_invalid_string(self):
        assert safe_decimal("N/A") is None

    def test_empty_string(self):
        assert safe_decimal("") is None

    # ── Sanitización de símbolos espurios del OCR ───────────────────────
    # Replica el comportamiento de phase4_ensamblador._limpiar_valor pero
    # aplicado YA en fase 3 fiscal, antes de validar aritmética. Si no se
    # limpia aquí, los signos espurios sobreviven al CSV de Intermega
    # porque la aritmética con negativos es internamente coherente.

    def test_strips_leading_minus_string(self):
        # OCR a veces lee un guion del layout como signo
        assert safe_decimal("-1313.94") == Decimal("1313.94")

    def test_strips_leading_plus_string(self):
        assert safe_decimal("+131.00") == Decimal("131.00")

    def test_strips_leading_percent_string(self):
        # "%" delante aparece cuando el OCR pega el símbolo al número
        assert safe_decimal("%21") == Decimal("21")

    def test_strips_whitespace_around_signed_string(self):
        assert safe_decimal("  -275.93  ") == Decimal("275.93")

    def test_placeholder_dash_returns_none(self):
        # Guion solo = OCR no encontró valor
        assert safe_decimal("-") is None

    def test_placeholder_double_dash_returns_none(self):
        assert safe_decimal("--") is None

    def test_placeholder_na_returns_none(self):
        assert safe_decimal("n/a") is None
        assert safe_decimal("NA") is None

    def test_negative_numeric_becomes_positive(self):
        # Defensa adicional: si llega un float/Decimal negativo (no string),
        # también lo neutralizamos. Las facturas que procesa la gestoría
        # nunca llevan importes negativos en su desglose de IVA.
        assert safe_decimal(-1313.94) == Decimal("1313.94")
        assert safe_decimal(Decimal("-275.93")) == Decimal("275.93")


class TestClasificarLinea:
    def test_gravada(self):
        cls, tipo = _clasificar_linea(21.0)
        assert cls == ClasificacionLinea.GRAVADA
        assert tipo == Decimal("21.0")

    def test_tipo_cero(self):
        cls, tipo = _clasificar_linea(0.0)
        assert cls == ClasificacionLinea.TIPO_CERO
        assert tipo == Decimal("0.0")

    def test_exenta(self):
        cls, tipo = _clasificar_linea(None)
        assert cls == ClasificacionLinea.EXENTA
        assert tipo is None

    def test_invalida(self):
        cls, tipo = _clasificar_linea("abc")
        assert cls == ClasificacionLinea.INVALIDA
        assert tipo is None


# ── Test de estructura compatible con ensamblador ────────────────────────────

class TestEstructuraEnsamblador:
    """Verifica que el resultado tiene la estructura que _extraer_campo_de_modulo espera."""

    def test_campos_total_euros_keys(self):
        fiscal = _fiscal(121.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        resultado = verificar_fiscal(fiscal)

        campo = resultado["campos"]["total_euros"]
        assert "valor_final" in campo
        assert "fuente_final" in campo
        assert "confianza_final" in campo
        assert "decision" in campo
        assert "motivo" in campo
        assert "llm_usado" in campo
        assert "candidatos" in campo
        assert campo["llm_usado"] is False

    def test_campos_lineas_fiscales_keys(self):
        fiscal = _fiscal(121.00, [_linea(100.00, 21.0, 21.00, 121.00)])
        resultado = verificar_fiscal(fiscal)

        campo = resultado["campos"]["lineas_fiscales"]
        assert "valor_final" in campo
        assert "fuente_final" in campo
        assert "confianza_final" in campo
        assert "decision" in campo
        assert campo["llm_usado"] is False
