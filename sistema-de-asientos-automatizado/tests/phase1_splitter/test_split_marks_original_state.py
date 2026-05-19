"""Integración del splitter con state_writer: marca sidecar original + inicializa splits."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import state_writer
from src.phase1_splitter import main as splitter_main
from src.phase1_splitter.main import ORIGINALES_DIRNAME, run_split


@pytest.fixture
def patched_env(monkeypatch, tmp_path):
    """Settings con audit_path, libros_base y asientos_path apuntando a tmp."""
    libros = tmp_path / "libros"
    inbox = libros / "facturas" / "compras"
    inbox.mkdir(parents=True)
    asientos = libros / "asientos"
    asientos.mkdir()
    audit = libros / "logs" / "audit"
    audit.mkdir(parents=True)
    cfg = SimpleNamespace(
        gemini_ocr_model="gemini-2.5-flash",
        audit_path=lambda: str(audit),
        asientos_path=lambda: str(asientos),
        libros_base=str(libros),
    )
    monkeypatch.setattr(splitter_main, "get_settings", lambda: cfg)
    return cfg, inbox, asientos


def _fake_client_response(facturas: list[dict]) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps({"facturas": facturas})
    )
    return client


def test_split_marks_original_sidecar_as_split_with_split_into(
    tmp_path, make_pdf, patched_env
):
    _cfg, inbox, asientos = patched_env
    pdf = inbox / "1_2_3_merged.pdf"
    make_pdf(pdf, 3)

    # Simulamos que el operario subió el PDF: hay un sidecar uploaded.
    original_folder = asientos / "compras_1_2_3_merged"
    state_writer.init_uploaded(
        original_folder,
        doc_id="1_2_3_merged",
        file_origin="facturas/compras/1_2_3_merged.pdf",
        sha256_file="a" * 64,
        size_bytes=12345,
    )

    client = _fake_client_response([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.9},
        {"paginas": [3], "confidence": 0.9},
    ])

    run_split(str(inbox), client=client)

    # Sidecar original ahora en estado split, con split_into completo.
    assert state_writer.is_split(original_folder)
    data = state_writer.read(original_folder)
    last = data["events"][-1]
    assert last["status"] == "split"
    assert last["split_into"] == [
        "1_2_3_merged__1of3",
        "1_2_3_merged__2of3",
        "1_2_3_merged__3of3",
    ]
    assert "archived_pdf" in last
    assert "_originales" in last["archived_pdf"]


def test_split_initializes_uploaded_sidecars_for_each_split(
    tmp_path, make_pdf, patched_env
):
    _cfg, inbox, asientos = patched_env
    pdf = inbox / "doc.pdf"
    make_pdf(pdf, 2)
    # No subimos sidecar previo (caso CLI sin upload UI) → splitter no debería
    # crear ni tocar nada para el original, pero SÍ inicializa los splits.

    client = _fake_client_response([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.9},
    ])

    run_split(str(inbox), client=client)

    # Cada split tiene su carpeta de asiento con .state.json uploaded.
    for idx in (1, 2):
        doc_id = f"doc__{idx}of2"
        folder = asientos / f"compras_{doc_id}"
        assert folder.exists(), f"missing folder for {doc_id}"
        assert state_writer.current_status(folder) == "uploaded"
        data = state_writer.read(folder)
        assert data["doc_id"] == doc_id


def test_split_no_original_sidecar_does_not_create_one(
    tmp_path, make_pdf, patched_env
):
    """Si no había sidecar para el original (caso CLI), el splitter no lo inventa."""
    _cfg, inbox, asientos = patched_env
    pdf = inbox / "solo.pdf"
    make_pdf(pdf, 2)

    client = _fake_client_response([
        {"paginas": [1], "confidence": 0.9},
        {"paginas": [2], "confidence": 0.9},
    ])

    run_split(str(inbox), client=client)

    # NO debe existir carpeta `compras_solo` (sin sidecar original previo).
    assert not (asientos / "compras_solo").exists()
    # Pero SÍ las de los splits.
    assert (asientos / "compras_solo__1of2").exists()
    assert (asientos / "compras_solo__2of2").exists()
