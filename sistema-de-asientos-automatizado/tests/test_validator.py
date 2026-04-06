"""
Tests para el módulo de validación de campos críticos.

Valida presencia y confianza mínima de campos requeridos.
"""

import pytest

from src.phase2_ocr import validator
import src.config as config


def create_complete_documento():
    """Helper para crear documento válido completo con nueva estructura fiscal."""
    return {
        "identificacion": {
            "numero_factura": {"valor": "F-001", "confianza": 0.97},
            "fecha_expedicion": {"valor": "2026-03-15", "confianza": 0.99},
            "nombre_entidad": {"valor": "Test SL", "confianza": 0.96},
            "nif_entidad": {"valor": "B12345678", "confianza": 0.95}
        },
        "fiscal": {
            "total_euros": {"valor": 129.04, "confianza": 0.99},
            "lineas_fiscales": [
                {
                    "base_euros": {"valor": 106.65, "confianza": 0.95},
                    "tipo_porcentaje": {"valor": 21.0, "confianza": 0.98},
                    "cuota": {"valor": 22.39, "confianza": 0.97},
                    "total_linea": {"valor": None, "confianza": None}
                }
            ]
        }
    }


def test_validate_complete_documento(monkeypatch):
    """Test validación de documento completo con alta confianza."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is True
    assert len(motivos) == 0


def test_validate_missing_numero_factura(monkeypatch):
    """Test fallo cuando numero_factura falta."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["identificacion"]["numero_factura"] = {"valor": None, "confianza": None}

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("numero_factura" in m for m in motivos)
    assert any("ausente" in m for m in motivos)


def test_validate_missing_nif_entidad(monkeypatch):
    """Test fallo cuando nif_entidad falta."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["identificacion"]["nif_entidad"] = {"valor": None, "confianza": None}

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("nif_entidad" in m for m in motivos)


def test_validate_low_confidence_numero_factura(monkeypatch):
    """Test fallo cuando confianza está por debajo del umbral."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["identificacion"]["numero_factura"]["confianza"] = 0.75

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("Confianza insuficiente" in m and "numero_factura" in m for m in motivos)


def test_validate_null_confidence_treated_as_failure(monkeypatch):
    """Test que confianza null también causa fallo."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["identificacion"]["numero_factura"]["confianza"] = None

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("Confianza insuficiente" in m for m in motivos)


def test_validate_multiple_failures_collected(monkeypatch):
    """Test que múltiples fallos se recolectan todos."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["identificacion"]["nif_entidad"]["valor"] = None
    doc["identificacion"]["numero_factura"]["confianza"] = 0.70

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert len(motivos) == 2
    assert any("nif_entidad" in m for m in motivos)
    assert any("numero_factura" in m for m in motivos)


def test_validate_total_euros_from_fiscal_root(monkeypatch):
    """Test que total_euros se valida desde fiscal root (nueva estructura)."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    doc["fiscal"]["total_euros"] = {"valor": None, "confianza": None}

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("total_euros" in m for m in motivos)


def test_validate_missing_total_euros_at_fiscal_root(monkeypatch):
    """Test manejo de documento sin total_euros en fiscal root."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    del doc["fiscal"]["total_euros"]

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is False
    assert any("total_euros" in m for m in motivos)


def test_get_nested_field_handles_missing_sections(monkeypatch):
    """Test que get_nested_field maneja secciones ausentes."""
    result = validator.get_nested_field({}, "identificacion", "numero_factura")

    assert result == {"valor": None, "confianza": None}


def test_validate_edge_case_confidence_exactly_at_threshold(monkeypatch):
    """Test que confianza exactamente en el umbral es válida."""
    class MockSettings:
        confianza_minima = 0.80

    monkeypatch.setattr(config, "_settings_instance", MockSettings())

    doc = create_complete_documento()
    for field in doc["identificacion"].values():
        field["confianza"] = 0.80
    doc["fiscal"]["total_euros"]["confianza"] = 0.80

    is_valid, motivos = validator.validate_documento_extraido(doc)

    assert is_valid is True
    assert len(motivos) == 0
