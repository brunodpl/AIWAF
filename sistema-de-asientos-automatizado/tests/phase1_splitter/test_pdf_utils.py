"""Tests de helpers PyMuPDF."""

from __future__ import annotations

import pytest

from src.phase1_splitter.pdf_utils import (
    extract_pages,
    page_count,
    sha256_file,
)


def test_page_count(tmp_path, make_pdf):
    pdf = tmp_path / "tres.pdf"
    make_pdf(pdf, 3)
    assert page_count(pdf) == 3


def test_sha256_is_deterministic(tmp_path, make_pdf):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    make_pdf(a, 2)
    # Copia byte-a-byte → mismo hash.
    b.write_bytes(a.read_bytes())
    assert sha256_file(a) == sha256_file(b)


def test_extract_single_page(tmp_path, make_pdf):
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    make_pdf(src, 3)
    extract_pages(src, [2], out)
    assert page_count(out) == 1


def test_extract_multiple_pages_in_order(tmp_path, make_pdf):
    src = tmp_path / "src.pdf"
    out = tmp_path / "out.pdf"
    make_pdf(src, 5)
    extract_pages(src, [2, 3, 4], out)
    assert page_count(out) == 3


def test_extract_rejects_out_of_range(tmp_path, make_pdf):
    src = tmp_path / "src.pdf"
    make_pdf(src, 2)
    with pytest.raises(ValueError, match="out of range"):
        extract_pages(src, [3], tmp_path / "x.pdf")


def test_extract_rejects_empty(tmp_path, make_pdf):
    src = tmp_path / "src.pdf"
    make_pdf(src, 1)
    with pytest.raises(ValueError, match="must not be empty"):
        extract_pages(src, [], tmp_path / "x.pdf")
