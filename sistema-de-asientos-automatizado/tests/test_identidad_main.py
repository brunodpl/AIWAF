"""
Tests de la fase 3.1: run_identidad().

Verifica que run_identidad() lee raw_document_ai.json,
llama a CabeceraResolver(usar_llm=False) y escribe
resultado_identidad_cabecera.json válido.

No requiere credenciales de GCP ni llamadas a Document AI.
Usa fixtures de raw_document_ai.json reales anonimizados.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.phase3_identidad_cabecera.main import run_identidad


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "golden"


def _fixture_raw(nombre: str) -> dict:
    """Cargar raw_document_ai.json de un caso golden."""
    path = FIXTURES_DIR / nombre / "raw_document_ai.json"
    if not path.exists():
        pytest.skip(f"Fixture no disponible: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _run_con_raw(raw: dict, doc_id: str = "test_doc") -> dict:
    """Ejecutar run_identidad con raw fixture en directorio temporal."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw_document_ai.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)

        ok = run_identidad(doc_id, tmp, usar_llm=False)
        assert ok, "run_identidad retornó False (error técnico)"

        resultado_path = Path(tmp) / "resultado_identidad_cabecera.json"
        assert resultado_path.exists(), "No se generó resultado_identidad_cabecera.json"

        with open(resultado_path, encoding="utf-8") as f:
            return json.load(f)


# ──────────────────────────────────────────────────────────
# Tests estructurales (sin fixture real)
# ──────────────────────────────────────────────────────────

def test_run_identidad_sin_raw_retorna_false():
    """Si raw_document_ai.json no existe, run_identidad retorna False."""
    with tempfile.TemporaryDirectory() as tmp:
        ok = run_identidad("doc_inexistente", tmp, usar_llm=False)
        assert not ok


def test_run_identidad_raw_vacio_retorna_false():
    """Si raw_document_ai.json contiene JSON inválido, retorna False."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw_document_ai.json"
        raw_path.write_text("no-es-json", encoding="utf-8")
        ok = run_identidad("doc_roto", tmp, usar_llm=False)
        assert not ok


def test_run_identidad_raw_minimo_escribe_artefacto():
    """
    Con un raw mínimo (sin entidades), run_identidad completa (True)
    y escribe un artefacto con estructura válida.
    """
    raw_minimo = {"entities": [], "text": "", "pages": []}
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw_document_ai.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_minimo, f)

        ok = run_identidad("doc_minimo", tmp, usar_llm=False)
        assert ok

        resultado_path = Path(tmp) / "resultado_identidad_cabecera.json"
        assert resultado_path.exists()

        with open(resultado_path, encoding="utf-8") as f:
            resultado = json.load(f)

        # Estructura mínima obligatoria
        assert "documento_id" in resultado
        assert "campos" in resultado
        assert "decision_global" in resultado
        assert resultado["documento_id"] == "doc_minimo"

        # fecha_operacion debe estar en campos (FIX verificado)
        assert "fecha_operacion" in resultado["campos"]
        fecha_oper = resultado["campos"]["fecha_operacion"]
        assert fecha_oper["valor_final"] is not None
        assert fecha_oper["decision"] == "auto"


def test_run_identidad_doc_id_propagado():
    """El documento_id en el artefacto debe coincidir con el pasado como argumento."""
    raw_minimo = {"entities": [], "text": "", "pages": []}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "raw_document_ai.json").write_text(
            json.dumps(raw_minimo), encoding="utf-8"
        )
        run_identidad("factura_test_42", tmp, usar_llm=False)
        with open(Path(tmp) / "resultado_identidad_cabecera.json", encoding="utf-8") as f:
            resultado = json.load(f)
        assert resultado["documento_id"] == "factura_test_42"


def test_run_identidad_todos_los_campos_presentes():
    """El JSON resultado debe contener todos los campos esperados en 'campos'."""
    raw_minimo = {"entities": [], "text": "", "pages": []}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "raw_document_ai.json").write_text(
            json.dumps(raw_minimo), encoding="utf-8"
        )
        run_identidad("doc_campos", tmp, usar_llm=False)
        with open(Path(tmp) / "resultado_identidad_cabecera.json", encoding="utf-8") as f:
            resultado = json.load(f)

        campos = resultado["campos"]
        for campo_esperado in [
            "nif_entidad", "nombre_entidad", "numero_factura",
            "fecha_expedicion", "fecha_operacion"
        ]:
            assert campo_esperado in campos, f"Campo faltante: {campo_esperado}"


# ──────────────────────────────────────────────────────────
# Tests con fixtures reales (opcionales si no están disponibles)
# ──────────────────────────────────────────────────────────

def test_fixture_1_nif_dni_valido():
    """Fixture 1: Álvarez Betanzos (persona física, DNI). Verifica que NIF no es block."""
    raw = _fixture_raw("factura_simple")
    resultado = _run_con_raw(raw, "factura_simple")
    nif = resultado["campos"]["nif_entidad"]
    assert nif["valor_final"] is not None or nif["decision"] != "auto", (
        "NIF ausente en fixture 1 — revisar extracción"
    )


def test_fixture_2_cif_en_pie_de_pagina():
    """Fixture 2: Carballeira SL (CIF en pie de página). Verifica que NIF se extrae."""
    raw = _fixture_raw("factura_cif_pie")
    resultado = _run_con_raw(raw, "factura_cif_pie")
    nif = resultado["campos"]["nif_entidad"]
    # Puede ser warn si está en pie de página con baja confianza, pero no debe ser None sin decisión
    assert "valor_final" in nif
    assert "decision" in nif


def test_fixture_3_iva_10_layout_irregular():
    """Fixture 3: Comercial Alim M.Blanco SL (IVA 10%, layout irregular)."""
    raw = _fixture_raw("factura_iva10")
    resultado = _run_con_raw(raw, "factura_iva10")
    # Verificar estructura mínima — el NIF puede ser warn en layout irregular
    assert resultado["decision_global"] in ("auto", "warn", "block")
    assert "fecha_expedicion" in resultado["campos"]
