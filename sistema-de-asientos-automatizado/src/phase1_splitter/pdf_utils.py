"""Helpers PyMuPDF para el splitter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

import fitz  # PyMuPDF


def page_count(pdf_path: str | Path) -> int:
    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


def sha256_file(pdf_path: str | Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pages(
    source_pdf: str | Path, pages_1based: Sequence[int], output_pdf: str | Path
) -> None:
    """Escribe en ``output_pdf`` un PDF con las páginas indicadas (1-based)
    extraídas de ``source_pdf``, en el orden dado.
    """
    if not pages_1based:
        raise ValueError("pages_1based must not be empty")
    with fitz.open(str(source_pdf)) as src:
        n = src.page_count
        for p in pages_1based:
            if p < 1 or p > n:
                raise ValueError(
                    f"page {p} out of range for {source_pdf} (has {n} pages)"
                )
        out = fitz.open()
        try:
            for p in pages_1based:
                out.insert_pdf(src, from_page=p - 1, to_page=p - 1)
            out.save(str(output_pdf))
        finally:
            out.close()


def read_bytes(pdf_path: str | Path) -> bytes:
    with open(pdf_path, "rb") as f:
        return f.read()
