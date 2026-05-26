"""Tests para ``split_single_file`` — punto de entrada del pre-scan en /upload.

``split_single_file`` debe:
- Devolver ``single_page`` sin llamar a Gemini si el PDF tiene 1 página.
- Devolver ``split`` (y crear N PDFs hijos) si Gemini detecta N>=2 facturas.
- Devolver ``single_factura`` si Gemini detecta N=1 en un PDF multi-página.
- Reintentar tras error transitorio de Gemini y devolver ``error`` tras
  agotar ``prescan_max_attempts`` intentos.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.phase1_splitter import main as splitter_main
from src.phase1_splitter.main import ORIGINALES_DIRNAME, split_single_file


@pytest.fixture
def patched_settings(monkeypatch, tmp_path):
    """Settings minimal incluyendo prescan_max_attempts y prescan_timeout_seconds.

    Forzamos backoff a 0 para que los tests con reintentos sean instantáneos.
    """
    audit = tmp_path / "audit"
    audit.mkdir()
    cfg = SimpleNamespace(
        gemini_ocr_model="gemini-2.5-flash",
        audit_path=lambda: str(audit),
        prescan_max_attempts=2,
        prescan_timeout_seconds=30,
        # Estos tests ejercitan el path Gemini → desactivamos el determinista.
        split_one_invoice_per_page=False,
    )
    monkeypatch.setattr(splitter_main, "get_settings", lambda: cfg)
    # Backoff a 0 → tests rápidos.
    monkeypatch.setattr(splitter_main, "_RETRY_BACKOFF_SECONDS", 0.0)
    return cfg, audit


def _fake_client(facturas: list[dict]) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"facturas": facturas})
    )
    return client


def test_single_page_returns_single_page_outcome_without_gemini(
    tmp_path, make_pdf, patched_settings
):
    pdf = tmp_path / "una.pdf"
    make_pdf(pdf, 1)
    client = MagicMock()

    outcome = split_single_file(pdf, client=client)

    assert outcome.status == "single_page"
    assert outcome.n_pages == 1
    assert outcome.n_facturas == 1
    client.models.generate_content.assert_not_called()


def test_multi_factura_produces_split_outputs(tmp_path, make_pdf, patched_settings):
    pdf = tmp_path / "lote.pdf"
    make_pdf(pdf, 3)
    client = _fake_client([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.85},
        {"paginas": [3], "confidence": 0.8},
    ])

    outcome = split_single_file(pdf, client=client)

    assert outcome.status == "split"
    assert outcome.n_facturas == 3
    assert outcome.n_pages == 3
    assert len(outcome.outputs) == 3
    assert (tmp_path / "lote__1of3.pdf").exists()
    assert (tmp_path / "lote__2of3.pdf").exists()
    assert (tmp_path / "lote__3of3.pdf").exists()
    # Original archivado.
    assert (tmp_path / ORIGINALES_DIRNAME / "lote.pdf").exists()
    assert not pdf.exists()


def test_single_factura_decision_leaves_pdf_untouched(
    tmp_path, make_pdf, patched_settings
):
    pdf = tmp_path / "dos.pdf"
    make_pdf(pdf, 2)
    client = _fake_client([
        {"paginas": [1, 2], "confidence": 0.92}
    ])

    outcome = split_single_file(pdf, client=client)

    assert outcome.status == "single_factura"
    assert outcome.n_pages == 2
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_gemini_retries_then_succeeds(tmp_path, make_pdf, patched_settings):
    """Primer intento falla, segundo intento devuelve respuesta válida."""
    pdf = tmp_path / "retry.pdf"
    make_pdf(pdf, 2)

    client = MagicMock()
    ok = SimpleNamespace(text=json.dumps({"facturas": [
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.9},
    ]}))
    client.models.generate_content.side_effect = [
        RuntimeError("transient 503"),
        ok,
    ]

    outcome = split_single_file(pdf, client=client)

    assert outcome.status == "split"
    assert client.models.generate_content.call_count == 2


def test_gemini_exhausts_retries_returns_error(tmp_path, make_pdf, patched_settings):
    pdf = tmp_path / "boom.pdf"
    make_pdf(pdf, 2)

    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("Vertex 500")

    outcome = split_single_file(pdf, client=client)

    assert outcome.status == "error"
    assert "Vertex 500" in outcome.error
    # 2 intentos (prescan_max_attempts del fixture).
    assert client.models.generate_content.call_count == 2
    # Sin escritura ni archivado del PDF original.
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_corrupt_pdf_returns_error_without_gemini(tmp_path, patched_settings):
    """PDF ilegible → fallo en page_count, no se llama a Gemini."""
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    client = MagicMock()

    outcome = split_single_file(bad, client=client)

    assert outcome.status == "error"
    assert "page_count" in outcome.error
    client.models.generate_content.assert_not_called()


def test_explicit_max_attempts_overrides_settings(tmp_path, make_pdf, patched_settings):
    """``max_attempts=1`` debe respetarse aunque settings diga 2."""
    pdf = tmp_path / "once.pdf"
    make_pdf(pdf, 2)
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("fail")

    outcome = split_single_file(pdf, client=client, max_attempts=1)

    assert outcome.status == "error"
    assert client.models.generate_content.call_count == 1
