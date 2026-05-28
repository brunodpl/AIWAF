"""
Tests para el servicio de cola de archivos.

Valida escaneo de carpetas, filtrado de archivos, movimiento y orden.
Incluye tests del contrato de subcarpetas por documento_id.
"""

import pytest
import os
import json
from unittest.mock import MagicMock

from src.phase2_ocr.file_queue_service import (
    scan_folder,
    save_json,
    ProcessingStats,
    run_ocr,
)
from src.phase2_ocr import file_queue_service as file_queue


def test_scan_folder_finds_valid_files(tmp_path, monkeypatch):
    """Test que scan_folder encuentra archivos válidos."""
    class MockSettings:
        extensiones_list = ["pdf", "jpg"]

    monkeypatch.setattr(file_queue, "get_settings", lambda: MockSettings())

    (tmp_path / "invoice1.pdf").touch()
    (tmp_path / "invoice2.jpg").touch()
    (tmp_path / "readme.txt").touch()
    (tmp_path / "subfolder").mkdir()

    files = scan_folder(str(tmp_path))

    assert len(files) == 2
    assert any("invoice1.pdf" in f for f in files)
    assert any("invoice2.jpg" in f for f in files)
    assert not any("readme.txt" in f for f in files)


def test_scan_folder_alphabetical_order(tmp_path, monkeypatch):
    """Test que archivos se retornan en orden alfabético."""
    class MockSettings:
        extensiones_list = ["pdf"]

    monkeypatch.setattr(file_queue, "get_settings", lambda: MockSettings())

    (tmp_path / "c.pdf").touch()
    (tmp_path / "a.pdf").touch()
    (tmp_path / "b.pdf").touch()

    files = scan_folder(str(tmp_path))
    basenames = [os.path.basename(f) for f in files]
    assert basenames == ["a.pdf", "b.pdf", "c.pdf"]


def test_scan_folder_raises_on_nonexistent_folder(monkeypatch):
    """Test que scan_folder lanza excepción si carpeta no existe."""
    class MockSettings:
        extensiones_list = ["pdf"]

    monkeypatch.setattr(file_queue, "get_settings", lambda: MockSettings())

    with pytest.raises(ValueError, match="does not exist"):
        scan_folder("/nonexistent/path")


def test_save_json(tmp_path):
    """Test guardado de JSON con encoding UTF-8."""
    data = {"test": "value", "número": 123, "emoji": "✅"}
    output_path = tmp_path / "test.json"

    save_json(data, str(output_path))

    assert output_path.exists()
    with open(output_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == data


def test_save_json_formats_with_indent(tmp_path):
    """Test que JSON se guarda con formato legible (indent)."""
    data = {"field1": "value1", "field2": "value2"}
    output_path = tmp_path / "test.json"

    save_json(data, str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert "\n" in content
    assert "  " in content


def test_processing_stats_initialization():
    stats = ProcessingStats()
    assert stats.total == 0
    assert stats.procesadas == 0
    assert stats.incidencias == 0
    assert stats.errores_tecnicos == 0


def test_processing_stats_print_summary(capsys):
    stats = ProcessingStats()
    stats.total = 10
    stats.procesadas = 7
    stats.incidencias = 2
    stats.errores_tecnicos = 1

    stats.print_summary("20_COMPRAS_GASTOS")

    captured = capsys.readouterr()
    assert "RESUMEN DE PROCESAMIENTO" in captured.out
    assert "20_COMPRAS_GASTOS" in captured.out
    assert "10" in captured.out
    assert "7" in captured.out


def test_scan_folder_empty_directory(tmp_path, monkeypatch):
    class MockSettings:
        extensiones_list = ["pdf"]

    monkeypatch.setattr(file_queue, "get_settings", lambda: MockSettings())

    files = scan_folder(str(tmp_path))
    assert len(files) == 0


def test_scan_folder_case_insensitive_extensions(tmp_path, monkeypatch):
    class MockSettings:
        extensiones_list = ["pdf"]

    monkeypatch.setattr(file_queue, "get_settings", lambda: MockSettings())

    (tmp_path / "invoice1.PDF").touch()
    (tmp_path / "invoice2.Pdf").touch()
    (tmp_path / "invoice3.pdf").touch()

    files = scan_folder(str(tmp_path))
    assert len(files) == 3


# ---------------------------------------------------------------------------
# Tests de run_ocr con nuevos clients (Vision + Gemini)
# ---------------------------------------------------------------------------

class TestRunOcr:
    """Tests para run_ocr (pipeline core sin mover archivos)."""

    def test_returns_doc_id_and_dir(self, tmp_path, monkeypatch):
        """run_ocr retorna (doc_id, doc_dir, is_valid, motivos)."""
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        invoice = input_dir / "factura_test.pdf"
        invoice.write_bytes(b"%PDF")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Mock Vision client
        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("Texto de factura", 1)

        # Mock Gemini model
        mock_gemini = MagicMock()

        # Mock mapper functions
        mock_doc = {
            "documento_id": "factura_test",
            "origen": {"archivo": "factura_test.pdf", "ocr_engine": "cloud_vision+gemini-2.5-flash"},
            "identificacion": {},
            "fiscal": {"total_euros": {"valor": None, "confianza": None}, "lineas_fiscales": []},
            "semantica": {"lineas_semanticas": []},
            "cliente_destino": {},
        }
        monkeypatch.setattr(file_queue, "estructurar_factura", lambda *a: {"total_factura": None})
        monkeypatch.setattr(file_queue, "construir_documento_extraido", lambda *a: dict(mock_doc))
        monkeypatch.setattr(file_queue, "validate_documento_extraido", lambda _: (True, []))

        doc_id, doc_dir, is_valid, motivos = run_ocr(
            str(invoice), "input", mock_vision, mock_gemini, str(output_dir)
        )

        assert doc_id == "factura_test"
        assert os.path.isdir(doc_dir)
        assert is_valid is True
        assert motivos == []
        assert os.path.exists(os.path.join(doc_dir, "documento_extraido.json"))

    def test_does_not_move_files(self, tmp_path, monkeypatch):
        """run_ocr no debe mover archivos."""
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        invoice = input_dir / "factura_nomove.pdf"
        invoice.write_bytes(b"%PDF")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("Texto", 1)
        mock_gemini = MagicMock()

        monkeypatch.setattr(file_queue, "estructurar_factura", lambda *a: {"total_factura": None})
        monkeypatch.setattr(file_queue, "construir_documento_extraido", lambda *a: {})
        monkeypatch.setattr(file_queue, "validate_documento_extraido", lambda _: (False, ["test"]))

        run_ocr(str(invoice), "input", mock_vision, mock_gemini, str(output_dir))

        assert invoice.exists()

    def test_gemini_failure_uses_fallback(self, tmp_path, monkeypatch):
        """Si Gemini falla, se usa json_minimos."""
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        invoice = input_dir / "factura_fallback.pdf"
        invoice.write_bytes(b"%PDF")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        mock_vision = MagicMock()
        mock_vision.extraer_texto.return_value = ("FACTURA 2026/001\nTotal: 100", 1)
        mock_gemini = MagicMock()

        # Gemini returns None (failure)
        monkeypatch.setattr(file_queue, "estructurar_factura", lambda *a: None)
        monkeypatch.setattr(file_queue, "validate_documento_extraido", lambda _: (False, ["fallback"]))

        doc_id, doc_dir, is_valid, motivos = run_ocr(
            str(invoice), "input", mock_vision, mock_gemini, str(output_dir)
        )

        # Verify json_minimos was used
        extraido_path = os.path.join(doc_dir, "documento_extraido.json")
        with open(extraido_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("error_extraccion") == "gemini_timeout_or_quota"
        assert data.get("requiere_revision") is True
