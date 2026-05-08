"""
Tests de integración del pipeline completo.

Verifica el flujo end-to-end sin llamadas reales a Cloud Vision o Gemini.
Usa mocks de VisionOcrClient y fixtures de JSON.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.phase2_ocr.file_queue_service import run_ocr


# ──────────────────────────────────────────────────────────
# Tests de run_ocr()
# ──────────────────────────────────────────────────────────

def test_run_ocr_retorna_doc_id_basename():
    """
    run_ocr debe usar el basename del archivo como documento_id (sin UUID).
    """
    with tempfile.TemporaryDirectory() as tmp_input, \
         tempfile.TemporaryDirectory() as tmp_output:

        factura_path = os.path.join(tmp_input, "factura_proveedor_001.pdf")
        Path(factura_path).write_bytes(b"%PDF-1.4 dummy")

        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("Texto factura", 1)
        mock_gemini = MagicMock()

        with patch("src.phase2_ocr.file_queue_service.estructurar_factura") as mock_struct, \
             patch("src.phase2_ocr.file_queue_service.construir_documento_extraido") as mock_build, \
             patch("src.phase2_ocr.file_queue_service.validate_documento_extraido") as mock_val:

            mock_struct.return_value = {"total_factura": None}
            mock_build.return_value = {
                "documento_id": "uuid-que-sera-sobreescrito",
                "identificacion": {},
                "fiscal": {"total_euros": {"valor": None, "confianza": None}, "lineas_fiscales": []},
                "semantica": {"lineas_semanticas": []},
                "cliente_destino": {},
                "origen": {},
            }
            mock_val.return_value = (False, ["Campo crítico ausente"])

            doc_id, doc_dir, is_valid, motivos = run_ocr(
                factura_path, "20_COMPRAS_GASTOS", mock_vision, mock_gemini, tmp_output
            )

        # Trazabilidad 2.0: la carpeta lleva prefijo de libro
        # ({libro_short}_{doc_id}) hasta que el orquestador la renombre.
        assert doc_id == "factura_proveedor_001"
        assert doc_dir == os.path.join(tmp_output, "compras_factura_proveedor_001")
        assert not is_valid
        assert len(motivos) > 0


def test_run_ocr_crea_subcarpeta_por_documento():
    """run_ocr debe crear data/output/{doc_id}/ y escribir los artefactos ahí."""
    with tempfile.TemporaryDirectory() as tmp_input, \
         tempfile.TemporaryDirectory() as tmp_output:

        factura_path = os.path.join(tmp_input, "mi_factura.pdf")
        Path(factura_path).write_bytes(b"%PDF-1.4 dummy")

        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("Texto", 1)
        mock_gemini = MagicMock()

        with patch("src.phase2_ocr.file_queue_service.estructurar_factura") as mock_struct, \
             patch("src.phase2_ocr.file_queue_service.construir_documento_extraido") as mock_build, \
             patch("src.phase2_ocr.file_queue_service.validate_documento_extraido") as mock_val:

            mock_struct.return_value = {"total_factura": None}
            mock_build.return_value = {
                "documento_id": "mi_factura",
                "identificacion": {},
                "fiscal": {"total_euros": {"valor": None, "confianza": None}, "lineas_fiscales": []},
                "semantica": {"lineas_semanticas": []},
                "cliente_destino": {},
                "origen": {},
            }
            mock_val.return_value = (True, [])

            doc_id, doc_dir, _, _ = run_ocr(
                factura_path, "20_COMPRAS_GASTOS", mock_vision, mock_gemini, tmp_output
            )

        doc_path = Path(tmp_output) / "compras_mi_factura"
        assert doc_path.is_dir()
        assert (doc_path / "documento_extraido.json").exists()
        assert (doc_path / ".state.json").exists()  # sidecar inicializado


def test_run_ocr_doc_id_sobreescribe_uuid_en_extraido():
    """El documento_id en documento_extraido.json debe ser el basename, no el UUID del mapper."""
    with tempfile.TemporaryDirectory() as tmp_input, \
         tempfile.TemporaryDirectory() as tmp_output:

        factura_path = os.path.join(tmp_input, "factura_estable.pdf")
        Path(factura_path).write_bytes(b"%PDF-1.4 dummy")

        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("Texto", 1)
        mock_gemini = MagicMock()

        with patch("src.phase2_ocr.file_queue_service.estructurar_factura") as mock_struct, \
             patch("src.phase2_ocr.file_queue_service.construir_documento_extraido") as mock_build, \
             patch("src.phase2_ocr.file_queue_service.validate_documento_extraido") as mock_val:

            mock_struct.return_value = {"total_factura": None}
            mock_build.return_value = {
                "documento_id": "uuid-aleatorio-generado-por-mapper",
                "identificacion": {},
                "fiscal": {"total_euros": {"valor": None, "confianza": None}, "lineas_fiscales": []},
                "semantica": {"lineas_semanticas": []},
                "cliente_destino": {},
                "origen": {},
            }
            mock_val.return_value = (True, [])

            run_ocr(factura_path, "20_COMPRAS_GASTOS", mock_vision, mock_gemini, tmp_output)

        extraido_path = Path(tmp_output) / "compras_factura_estable" / "documento_extraido.json"
        with open(extraido_path, encoding="utf-8") as f:
            extraido = json.load(f)

        assert extraido["documento_id"] == "factura_estable"


# ──────────────────────────────────────────────────────────
# Tests de integración pipeline e2e (sin Vision/Gemini real)
# ──────────────────────────────────────────────────────────

def test_pipeline_e2e_genera_resultado_validacion():
    """
    Flujo completo: folder con 1 factura → resultado_validacion.json generado.
    Mock de VisionOcrClient y Gemini. Sin llamadas a GCP.
    """
    with tempfile.TemporaryDirectory() as tmp_folder, \
         tempfile.TemporaryDirectory() as tmp_output:

        factura_path = os.path.join(tmp_folder, "factura_e2e.pdf")
        Path(factura_path).write_bytes(b"%PDF-1.4 dummy")

        with patch("src.pipeline.VisionOcrClient") as MockVision, \
             patch("src.pipeline._init_gemini_model") as MockGemini, \
             patch("src.pipeline.get_settings") as mock_cfg, \
             patch("src.phase2_ocr.file_queue_service.get_settings") as mock_cfg2, \
             patch("src.phase2_ocr.file_queue_service.get_global_settings") as mock_cfg3, \
             patch("src.phase2_ocr.file_queue_service.estructurar_factura") as mock_struct, \
             patch("src.phase2_ocr.file_queue_service.construir_documento_extraido") as mock_build, \
             patch("src.phase2_ocr.file_queue_service.validate_documento_extraido") as mock_val:

            # Vision mock
            mock_vision_inst = MagicMock()
            mock_vision_inst.extraer_texto.return_value = ("Texto factura", 1)
            MockVision.return_value = mock_vision_inst
            MockGemini.return_value = MagicMock()

            cfg = MagicMock()
            cfg.output_path = tmp_output
            cfg.extensiones_list = ["pdf"]
            cfg.logs_path = str(Path(tmp_output) / "logs")
            cfg.asientos_path.return_value = tmp_output
            cfg.audit_path.return_value = str(Path(tmp_output) / "logs" / "audit")
            mock_cfg.return_value = cfg
            mock_cfg2.return_value = cfg
            mock_cfg3.return_value = cfg

            mock_struct.return_value = {"total_factura": None}
            mock_build.return_value = {
                "documento_id": "factura_e2e",
                "identificacion": {},
                "fiscal": {"total_euros": {"valor": None, "confianza": None}, "lineas_fiscales": []},
                "semantica": {"lineas_semanticas": []},
                "cliente_destino": {},
                "origen": {},
            }
            mock_val.return_value = (False, ["Sin campos críticos"])

            from src.pipeline import run_pipeline
            summary = run_pipeline(tmp_folder, "20_COMPRAS_GASTOS")

        assert summary["total"] == 1

        # Trazabilidad 2.0: carpeta lleva prefijo de libro
        carpeta_asiento = Path(tmp_output) / "compras_factura_e2e"
        resultado_path = carpeta_asiento / "resultado_validacion.json"
        sidecar_path = carpeta_asiento / ".state.json"
        assert resultado_path.exists(), "resultado_validacion.json no fue generado"
        assert sidecar_path.exists(), ".state.json no fue generado"

        with open(resultado_path, encoding="utf-8") as f:
            resultado = json.load(f)

        assert resultado["documento_id"] == "factura_e2e"
        assert resultado["schema_version"] == "v1"
        assert resultado["libro"] == "20_COMPRAS_GASTOS"
        assert "decision_global" in resultado
        assert "campos" in resultado

        # PDF original NO se mueve (cero shutil.move en pipeline 2.0)
        assert Path(factura_path).exists(), "El PDF original no debe moverse"
