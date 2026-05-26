"""Tests del split determinista (1 página = 1 factura, sin Gemini).

Con ``split_one_invoice_per_page=True`` (default), un PDF multi-página se
trocea en N facturas de 1 página cada una de forma 100% reproducible, SIN
llamar a Gemini Vision. Si una página no fuera una factura válida, el pipeline
la bloquea como incidencia aguas abajo y sigue con las demás.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.phase1_splitter import main as splitter_main
from src.phase1_splitter.main import ORIGINALES_DIRNAME, split_single_file


def _boom_gemini(*args, **kwargs):
    raise AssertionError("Gemini NO debe usarse en modo determinista")


@pytest.fixture
def patched_settings_det(monkeypatch, tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    cfg = SimpleNamespace(
        gemini_ocr_model="gemini-2.5-flash",
        audit_path=lambda: str(audit),
        prescan_max_attempts=2,
        prescan_timeout_seconds=30,
        split_one_invoice_per_page=True,
    )
    monkeypatch.setattr(splitter_main, "get_settings", lambda: cfg)
    # Si el split determinista fallara y cayera al path Gemini, esto lo delata.
    monkeypatch.setattr(splitter_main, "_init_gemini_model", _boom_gemini)
    monkeypatch.setattr(splitter_main, "_RETRY_BACKOFF_SECONDS", 0.0)
    return cfg, audit


def test_split_determinista_una_factura_por_pagina(tmp_path, make_pdf, patched_settings_det):
    pdf = tmp_path / "lote.pdf"
    make_pdf(pdf, 3)

    # Sin cliente: en determinista no se necesita Gemini.
    outcome = split_single_file(pdf)

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


def test_split_determinista_no_llama_a_gemini_en_pdf_grande(tmp_path, make_pdf, patched_settings_det):
    pdf = tmp_path / "grande.pdf"
    make_pdf(pdf, 12)

    # Si tocara Gemini, _init_gemini_model lanzaría AssertionError.
    outcome = split_single_file(pdf)

    assert outcome.status == "split"
    assert outcome.n_facturas == 12
    assert len(outcome.outputs) == 12
    # Confianza determinista = 1.0.
    assert outcome.confidence_min == 1.0


def test_split_determinista_una_pagina_es_single_page(tmp_path, make_pdf, patched_settings_det):
    pdf = tmp_path / "una.pdf"
    make_pdf(pdf, 1)

    outcome = split_single_file(pdf)

    assert outcome.status == "single_page"
    assert outcome.n_facturas == 1
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_deterministic_page_split_builder():
    decision = splitter_main._deterministic_page_split(4)
    assert len(decision.groups) == 4
    assert [g.pages for g in decision.groups] == [(1,), (2,), (3,), (4,)]
    assert decision.model_used == "deterministic_page_split"
    assert all(g.confidence == 1.0 for g in decision.groups)
