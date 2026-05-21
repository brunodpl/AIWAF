"""Tests de integración del orquestador run_split."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.phase1_splitter import main as splitter_main
from src.phase1_splitter.main import ORIGINALES_DIRNAME, run_split
from src.phase1_splitter.pdf_utils import page_count


@pytest.fixture
def patched_settings(monkeypatch, tmp_path):
    """Settings minimal con audit_path apuntando a tmp."""
    audit = tmp_path / "audit"
    audit.mkdir()
    cfg = SimpleNamespace(
        gemini_ocr_model="gemini-2.5-flash",
        audit_path=lambda: str(audit),
        # Pre-scan: 1 intento es suficiente para los tests del orquestador
        # (estos no validan reintentos — eso vive en test_split_single_file).
        prescan_max_attempts=1,
        prescan_timeout_seconds=30,
    )
    monkeypatch.setattr(splitter_main, "get_settings", lambda: cfg)
    return cfg, audit


def _fake_client_response(facturas: list[dict]) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"facturas": facturas})
    )
    return client


def test_run_split_skips_single_page_pdfs(tmp_path, make_pdf, patched_settings):
    pdf = tmp_path / "una.pdf"
    make_pdf(pdf, 1)
    client = MagicMock()  # No debe llamarse.

    outcomes = run_split(str(tmp_path), client=client)

    assert len(outcomes) == 1
    assert outcomes[0].status == "single_page"
    assert outcomes[0].n_pages == 1
    client.models.generate_content.assert_not_called()
    # PDF no se mueve.
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_run_split_three_facturas_writes_outputs_and_archives_original(
    tmp_path, make_pdf, patched_settings
):
    pdf = tmp_path / "1_2_3_merged.pdf"
    make_pdf(pdf, 3)
    client = _fake_client_response([
        {"paginas": [1], "emisor_cif_aparente": "A1", "confidence": 0.95},
        {"paginas": [2], "emisor_cif_aparente": "B2", "confidence": 0.88},
        {"paginas": [3], "emisor_cif_aparente": "C3", "confidence": 0.91},
    ])

    outcomes = run_split(str(tmp_path), client=client)

    [outcome] = outcomes
    assert outcome.status == "split"
    assert outcome.n_facturas == 3
    assert outcome.n_pages == 3
    assert outcome.confidence_min == pytest.approx(0.88)

    # Tres PDFs nuevos, cada uno de 1 página.
    expected = [
        tmp_path / "1_2_3_merged__1of3.pdf",
        tmp_path / "1_2_3_merged__2of3.pdf",
        tmp_path / "1_2_3_merged__3of3.pdf",
    ]
    for p in expected:
        assert p.exists(), f"missing {p.name}"
        assert page_count(p) == 1

    # Original archivado.
    archived = tmp_path / ORIGINALES_DIRNAME / "1_2_3_merged.pdf"
    assert archived.exists()
    assert not pdf.exists()

    # Manifiesto JSON con metadatos.
    manifest_path = tmp_path / ORIGINALES_DIRNAME / "1_2_3_merged.split.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["origen_pdf"] == "1_2_3_merged.pdf"
    assert manifest["n_facturas"] == 3
    assert manifest["origen_paginas"] == 3
    assert len(manifest["facturas"]) == 3
    assert manifest["facturas"][0]["paginas_originales"] == [1]
    assert manifest["facturas"][1]["emisor_cif_aparente"] == "B2"


def test_run_split_single_factura_response_leaves_pdf_untouched(
    tmp_path, make_pdf, patched_settings
):
    """Si Gemini concluye N=1 sobre un PDF multi-página, no movemos nada."""
    pdf = tmp_path / "dos.pdf"
    make_pdf(pdf, 2)
    client = _fake_client_response([
        {"paginas": [1, 2], "emisor_cif_aparente": "X", "confidence": 0.9}
    ])

    outcomes = run_split(str(tmp_path), client=client)

    assert outcomes[0].status == "single_factura"
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_run_split_gemini_error_leaves_pdf_untouched(
    tmp_path, make_pdf, patched_settings
):
    pdf = tmp_path / "dos.pdf"
    make_pdf(pdf, 2)
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("Vertex 500")

    outcomes = run_split(str(tmp_path), client=client)

    assert outcomes[0].status == "error"
    assert "Vertex 500" in outcomes[0].error
    assert pdf.exists()
    assert not (tmp_path / ORIGINALES_DIRNAME).exists()


def test_run_split_idempotent_ignores_originales_subfolder(
    tmp_path, make_pdf, patched_settings
):
    """Segunda corrida no re-procesa los PDFs ya archivados en _originales/."""
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, 3)
    client = _fake_client_response([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2, 3], "confidence": 0.9},
    ])

    run_split(str(tmp_path), client=client)
    # Reseteamos contadores del mock: en la segunda corrida los outputs son de
    # 1 y 2 páginas. El de 2 páginas SÍ vuelve a llamar a Gemini; el de 1
    # página no. El original archivado NO debe verse.
    client.models.generate_content.reset_mock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"facturas": [{"paginas": [1, 2], "confidence": 0.9}]})
    )

    run_split(str(tmp_path), client=client)

    # Solo 1 llamada nueva: la del output de 2 páginas.
    assert client.models.generate_content.call_count == 1


def test_run_split_writes_audit_log(tmp_path, make_pdf, patched_settings):
    _cfg, audit_dir = patched_settings
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, 2)
    client = _fake_client_response([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.8},
    ])

    run_split(str(tmp_path), client=client)

    logs = list(audit_dir.glob("splits_*.jsonl"))
    assert len(logs) == 1
    lines = logs[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["status"] == "split"
    assert rec["n_facturas"] == 2
    assert rec["confidence_min"] == pytest.approx(0.8)


def test_run_split_raises_on_missing_folder(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        run_split(str(tmp_path / "does_not_exist"))
