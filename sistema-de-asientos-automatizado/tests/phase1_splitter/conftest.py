"""Fixtures compartidas para tests del splitter."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest


def _make_pdf(path: Path, n_pages: int) -> None:
    doc = fitz.open()
    try:
        for i in range(n_pages):
            page = doc.new_page(width=595, height=842)  # A4 portrait
            page.insert_text((72, 72), f"PAGE {i + 1}")
        doc.save(str(path))
    finally:
        doc.close()


@pytest.fixture
def make_pdf():
    """Devuelve una función para crear PDFs sintéticos en disco."""
    return _make_pdf
