"""
Tests para el módulo de mapeo Gemini → JSON interno.

Valida transformación de datos Gemini a documento_extraido.json schema,
asignación de confianzas deterministas y fallback de mínimos.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import date, timezone, datetime

from src.phase2_ocr import mapper_document_ai_to_json as mapper
from src.phase2_ocr.mapper_document_ai_to_json import (
    _confianza_campo,
    validar_suma_fiscal,
    construir_documento_extraido,
    construir_prompt_gemini,
    json_minimos,
    CONFIANZA_CAMPO_OK,
    CONFIANZA_CAMPO_KO,
    CONFIANZA_TOTAL_WARN,
)


# ─── _init_gemini_model: timeout (bug del lote colgado) ──────────────────

def test_init_gemini_model_aplica_timeout_desde_config():
    """El cliente Gemini debe llevar timeout (en ms) tomado de config.

    Sin timeout, una llamada colgada bloquea el lote entero (un cuelgue no
    lanza excepción → el bucle de reintentos no actúa → el lote no avanza).
    """
    cfg = SimpleNamespace(
        google_application_credentials=None,
        google_cloud_project_id="proj-test",
        gemini_ocr_location="europe-west1",
        gemini_ocr_model="gemini-2.5-flash",
        gemini_ocr_timeout_seconds=120,
    )
    with patch.object(mapper, "get_settings", return_value=cfg), \
            patch.object(mapper.genai, "Client") as mock_client:
        mapper._init_gemini_model()

    http_options = mock_client.call_args.kwargs["http_options"]
    assert http_options.timeout == 120 * 1000


# ─── _confianza_campo ──────────���────────────────────────────────────────

def test_confianza_campo_presente():
    assert _confianza_campo("B12345678") == CONFIANZA_CAMPO_OK


def test_confianza_campo_none():
    assert _confianza_campo(None) == CONFIANZA_CAMPO_KO


def test_confianza_campo_empty_string():
    assert _confianza_campo("") == CONFIANZA_CAMPO_KO


def test_confianza_campo_whitespace():
    assert _confianza_campo("   ") == CONFIANZA_CAMPO_KO


def test_confianza_campo_number():
    assert _confianza_campo(129.04) == CONFIANZA_CAMPO_OK


def test_confianza_campo_nan():
    assert _confianza_campo(float("nan")) == CONFIANZA_CAMPO_KO


# ─── validar_suma_fiscal ─────────��──────────────────────────────────────

def test_validar_suma_fiscal_ok():
    lineas = [{"base_euros": 100.00, "cuota": 21.00}]
    assert validar_suma_fiscal(lineas, 121.00) is True


def test_validar_suma_fiscal_tolerance():
    """Tolerancia de 0.02€ por redondeo."""
    lineas = [{"base_euros": 100.00, "cuota": 21.00}]
    assert validar_suma_fiscal(lineas, 121.01) is True


def test_validar_suma_fiscal_fails():
    lineas = [{"base_euros": 100.00, "cuota": 21.00}]
    assert validar_suma_fiscal(lineas, 130.00) is False


def test_validar_suma_fiscal_empty_lines():
    """Sin líneas, no penalizar."""
    assert validar_suma_fiscal([], 100.00) is True


def test_validar_suma_fiscal_null_total():
    """Sin total, no penalizar."""
    lineas = [{"base_euros": 100.00, "cuota": 21.00}]
    assert validar_suma_fiscal(lineas, None) is True


def test_validar_suma_fiscal_multi_iva():
    """Factura con dos tramos de IVA."""
    lineas = [
        {"base_euros": 24.00, "cuota": 5.04},
        {"base_euros": 125.00, "cuota": 5.00},
    ]
    assert validar_suma_fiscal(lineas, 159.04) is True


def test_validar_suma_fiscal_partial_lines():
    """Línea con base None se ignora en la suma."""
    lineas = [
        {"base_euros": 100.00, "cuota": 21.00},
        {"base_euros": None, "cuota": None},
    ]
    assert validar_suma_fiscal(lineas, 121.00) is True


# ─── construir_prompt_gemini ────────────────────────────────────────────

def test_construir_prompt_contiene_texto():
    prompt = construir_prompt_gemini("FACTURA F-001\nTotal: 121.00€")
    assert "FACTURA F-001" in prompt
    assert "Total: 121.00€" in prompt


def test_construir_prompt_contiene_instrucciones():
    prompt = construir_prompt_gemini("texto")
    assert "lineas_fiscales" in prompt
    assert "YYYY-MM-DD" in prompt
    assert "nif_entidad" in prompt


# ─── construir_documento_extraido ───────────────────────────────────────

def _gemini_completa():
    """Helper: datos Gemini de factura completa."""
    return {
        "numero_factura": "F-2026-001",
        "fecha_expedicion": "2026-03-15",
        "nombre_entidad": "Bebidas García S.L.",
        "nif_entidad": "B12345678",
        "nombre_receptor": "Restaurante Pepe S.L.",
        "nif_receptor": "B20091754",
        "total_factura": 159.04,
        "lineas_fiscales": [
            {"base_euros": 24.00, "tipo_porcentaje": 21.0, "cuota": 5.04, "total_linea": 29.04},
            {"base_euros": 125.00, "tipo_porcentaje": 4.0, "cuota": 5.00, "total_linea": 130.00},
        ]
    }


def test_construir_documento_completo():
    result = construir_documento_extraido(_gemini_completa(), "factura.pdf", 1, "20_COMPRAS_GASTOS")

    assert "documento_id" in result
    assert result["origen"]["archivo"] == "factura.pdf"
    assert result["origen"]["pagina_count"] == 1
    assert result["origen"]["ocr_engine"] == "cloud_vision+gemini-2.5-flash"
    assert result["origen"]["carpeta_entrada"] == "20_COMPRAS_GASTOS"


def test_construir_documento_identificacion():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    ident = result["identificacion"]
    assert ident["numero_factura"]["valor"] == "F-2026-001"
    assert ident["numero_factura"]["confianza"] == CONFIANZA_CAMPO_OK
    assert ident["nif_entidad"]["valor"] == "B12345678"
    assert ident["nif_entidad"]["confianza"] == CONFIANZA_CAMPO_OK
    # F3: fecha_operacion deriva de fecha_expedicion; ya no se fabrica con conf 1.0.
    assert ident["fecha_operacion"]["valor"] == ident["fecha_expedicion"]["valor"]
    assert ident["fecha_operacion"]["confianza"] == ident["fecha_expedicion"]["confianza"]
    assert ident["fecha_operacion"]["confianza"] != 1.0


def test_construir_documento_cliente_destino():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    cliente = result["cliente_destino"]
    assert cliente["nif_receptor"]["valor"] == "B20091754"
    assert cliente["nif_receptor"]["confianza"] == CONFIANZA_CAMPO_OK
    assert cliente["nombre_receptor"]["valor"] == "Restaurante Pepe S.L."


def test_construir_documento_fiscal():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    fiscal = result["fiscal"]
    assert fiscal["total_euros"]["valor"] == 159.04
    assert fiscal["total_euros"]["confianza"] == CONFIANZA_CAMPO_OK
    assert fiscal["requiere_revision"] is False
    assert len(fiscal["lineas_fiscales"]) == 2


def test_construir_documento_lineas_fiscales_confianza():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    linea = result["fiscal"]["lineas_fiscales"][0]
    assert linea["base_euros"]["valor"] == 24.00
    assert linea["base_euros"]["confianza"] == CONFIANZA_CAMPO_OK
    assert linea["tipo_porcentaje"]["valor"] == 21.0
    assert linea["cuota"]["valor"] == 5.04


def test_construir_documento_semantica_placeholder():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    sem = result["semantica"]["lineas_semanticas"]
    assert len(sem) == 1
    assert sem[0]["concepto_raw"]["valor"] is None
    assert sem[0]["concepto_normalizado"] is None


def test_construir_documento_campos_null():
    """Campos ausentes en Gemini producen confianza 0.0."""
    gemini = {
        "numero_factura": None,
        "fecha_expedicion": None,
        "nombre_entidad": None,
        "nif_entidad": None,
        "nombre_receptor": None,
        "nif_receptor": None,
        "total_factura": None,
        "lineas_fiscales": [],
    }
    result = construir_documento_extraido(gemini, "f.pdf", 1, "20")

    assert result["identificacion"]["nif_entidad"]["confianza"] == CONFIANZA_CAMPO_KO
    assert result["fiscal"]["total_euros"]["confianza"] == CONFIANZA_CAMPO_OK  # no lineas + null total → suma_ok=True


def test_construir_documento_suma_no_cuadra():
    """Si la suma fiscal no cuadra, total_euros tiene confianza 0.5."""
    gemini = _gemini_completa()
    gemini["total_factura"] = 999.99  # No cuadra con líneas

    result = construir_documento_extraido(gemini, "f.pdf", 1, "20")

    assert result["fiscal"]["total_euros"]["confianza"] == CONFIANZA_TOTAL_WARN
    assert result["fiscal"]["requiere_revision"] is True


def test_construir_documento_linea_sin_base_ni_tipo():
    """Línea fiscal sin base o tipo produce confianza 0.0."""
    gemini = _gemini_completa()
    gemini["lineas_fiscales"] = [
        {"base_euros": None, "tipo_porcentaje": None, "cuota": 5.00, "total_linea": None},
    ]

    result = construir_documento_extraido(gemini, "f.pdf", 1, "20")

    linea = result["fiscal"]["lineas_fiscales"][0]
    assert linea["base_euros"]["confianza"] == CONFIANZA_CAMPO_KO
    assert linea["cuota"]["confianza"] == CONFIANZA_CAMPO_KO


def test_construir_documento_fecha_operacion_hoy():
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")
    fecha = result["identificacion"]["fecha_operacion"]["valor"]
    assert len(fecha) == 10
    assert fecha[4] == "-"
    assert fecha[7] == "-"


# ─── Estructura fiscal v2 ──────────────────────────────────────────────

def test_fiscal_structure_v2():
    """total_euros at fiscal root, lineas_fiscales sin total_euros."""
    result = construir_documento_extraido(_gemini_completa(), "f.pdf", 1, "20")

    assert "total_euros" in result["fiscal"]
    assert result["fiscal"]["total_euros"]["valor"] == 159.04

    for linea in result["fiscal"]["lineas_fiscales"]:
        assert "total_euros" not in linea
        assert "base_euros" in linea
        assert "tipo_porcentaje" in linea
        assert "cuota" in linea
        assert "total_linea" in linea


# ─── json_minimos ───��───────────────────────────────────────────────────

def test_json_minimos_basic():
    result = json_minimos("factura.pdf", "20_COMPRAS_GASTOS")

    assert result["origen"]["archivo"] == "factura.pdf"
    assert result["origen"]["ocr_engine"] == "cloud_vision+gemini-2.5-flash"
    assert result["requiere_revision"] is True
    assert result["error_extraccion"] == "gemini_timeout_or_quota"
    assert result["identificacion"]["nif_entidad"]["confianza"] == 0.0


def test_json_minimos_rescata_numero_factura():
    texto = "FACTURA 2026/0045\nFecha: 15/03/2026"
    result = json_minimos("f.pdf", "20", texto)

    assert result["identificacion"]["numero_factura"]["valor"] == "2026/0045"
    assert result["identificacion"]["numero_factura"]["confianza"] == 0.5


def test_json_minimos_sin_numero():
    result = json_minimos("f.pdf", "20", "texto sin número")

    assert result["identificacion"]["numero_factura"]["valor"] is None
    assert result["identificacion"]["numero_factura"]["confianza"] == 0.0
