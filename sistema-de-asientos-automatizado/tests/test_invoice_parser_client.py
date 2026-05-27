"""Tests para VisionOcrClient — troceo de PDFs multipágina (Cloud Vision síncrono).

Cubre el fallo de la factura Gadis multipágina: la ruta inline de Cloud Vision
limita los PDF a 5 páginas, así que un PDF de >5 páginas debe trocearse en lotes
de ≤límite y concatenarse con numeración de página global continua.
"""

import fitz
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.phase2_ocr import invoice_parser_client as ipc


def _make_pdf_bytes(n_pages: int) -> bytes:
    """PDF en memoria con ``n_pages`` páginas en blanco."""
    doc = fitz.open()
    try:
        for _ in range(n_pages):
            doc.new_page()
        return doc.tobytes()
    finally:
        doc.close()


def _page_count(content: bytes) -> int:
    with fitz.open(stream=content, filetype="pdf") as d:
        return d.page_count


def _make_client_with_mock(batch_side_effect) -> "ipc.VisionOcrClient":
    """Instancia sin __init__ (no necesita credenciales) con el cliente mockeado."""
    client = ipc.VisionOcrClient.__new__(ipc.VisionOcrClient)
    client.client = MagicMock()
    client.client.batch_annotate_files.side_effect = batch_side_effect
    client.max_retries = 1
    client.vision_sync_page_limit = 5
    client.vision_timeout_seconds = 120
    return client


def _ok_response_for(requests, timeout=None):
    """side_effect: una página OK por cada página real del chunk recibido."""
    req = requests[0]
    content = req.input_config.content
    n = _page_count(content)
    page_responses = [
        SimpleNamespace(
            error=SimpleNamespace(message=""),
            full_text_annotation=SimpleNamespace(text="texto"),
        )
        for _ in range(n)
    ]
    file_response = SimpleNamespace(error=SimpleNamespace(message=""), responses=page_responses)
    return SimpleNamespace(responses=[file_response])


class TestPdfPageChunks:
    def test_trocea_7_paginas_en_lotes_de_5(self):
        content = _make_pdf_bytes(7)
        chunks = ipc._pdf_page_chunks(content, 5)
        assert [_page_count(c) for c in chunks] == [5, 2]

    def test_no_trocea_cuando_cabe_en_un_lote(self):
        content = _make_pdf_bytes(3)
        chunks = ipc._pdf_page_chunks(content, 5)
        assert len(chunks) == 1
        assert _page_count(chunks[0]) == 3


class TestExtractTextPdfChunking:
    def test_pdf_de_7_paginas_hace_dos_llamadas_de_max_5(self):
        client = _make_client_with_mock(_ok_response_for)
        content = _make_pdf_bytes(7)

        texto, num_paginas = client._extract_text_pdf(content)

        assert client.client.batch_annotate_files.call_count == 2
        # Cada request lleva ≤5 páginas.
        for call in client.client.batch_annotate_files.call_args_list:
            req = call.kwargs["requests"][0]
            assert _page_count(req.input_config.content) <= 5
        # Numeración global continua 1..7, en orden, sin 8.
        assert num_paginas == 7
        for n in range(1, 8):
            assert f"--- PÁGINA {n} ---" in texto
        assert "--- PÁGINA 8 ---" not in texto
        assert texto.index("--- PÁGINA 1 ---") < texto.index("--- PÁGINA 7 ---")

    def test_pdf_de_3_paginas_una_sola_llamada(self):
        client = _make_client_with_mock(_ok_response_for)
        content = _make_pdf_bytes(3)

        texto, num_paginas = client._extract_text_pdf(content)

        assert client.client.batch_annotate_files.call_count == 1
        assert num_paginas == 3

    def test_batch_annotate_recibe_timeout(self):
        # Sin timeout en la llamada a Cloud Vision, un cuelgue bloquea el lote.
        client = _make_client_with_mock(_ok_response_for)
        client.vision_timeout_seconds = 99
        content = _make_pdf_bytes(2)

        client._extract_text_pdf(content)

        for call in client.client.batch_annotate_files.call_args_list:
            assert call.kwargs["timeout"] == 99

    def test_error_a_nivel_fichero_lanza_excepcion(self):
        def _file_error(requests, timeout=None):
            file_response = SimpleNamespace(
                error=SimpleNamespace(message="Document exceeds page limit"),
                responses=[],
            )
            return SimpleNamespace(responses=[file_response])

        client = _make_client_with_mock(_file_error)
        content = _make_pdf_bytes(2)

        with pytest.raises(Exception, match="exceeds page limit"):
            client._extract_text_pdf(content)
