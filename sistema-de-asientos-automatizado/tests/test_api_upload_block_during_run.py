"""Tests del guard 409 en POST /api/books/{book_id}/upload durante pipeline run."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main


@pytest.fixture
def libros_root(tmp_path: Path) -> Path:
    root = tmp_path / "libros"
    (root / "facturas" / "compras").mkdir(parents=True)
    (root / "facturas" / "ventas").mkdir(parents=True)
    (root / "facturas" / "bienes").mkdir(parents=True)
    (root / "asientos").mkdir(parents=True)
    (root / "logs" / "audit").mkdir(parents=True)
    (root / ".runtime").mkdir(parents=True)
    return root


@pytest.fixture
def client(monkeypatch, libros_root):
    base = str(libros_root)
    cfg = SimpleNamespace(
        libros_base=base,
        output_path=str(libros_root.parent / "data" / "output"),
        logs_path=str(libros_root / "logs"),
        sandbox_base_path=str(libros_root.parent / "sandbox"),
        inbox_path=lambda libro: str(libros_root / "facturas" / libro),
        asientos_path=lambda: str(libros_root / "asientos"),
        runtime_path=lambda: str(libros_root / ".runtime"),
        audit_path=lambda: str(libros_root / "logs" / "audit"),
    )
    monkeypatch.setattr(api_main, "settings", lambda: cfg)
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    return TestClient(api_main.app), libros_root


def _fake_pdf() -> tuple[str, io.BytesIO, str]:
    # Minimal valid PDF bytes (header + EOF) for upload validation.
    data = b"%PDF-1.4\n%EOF\n"
    return ("factura.pdf", io.BytesIO(data), "application/pdf")


def test_upload_returns_409_when_pipeline_lock_active(client, monkeypatch):
    c, _root = client
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: True)

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", _fake_pdf())],
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "pipeline_running"
    assert "procesado en curso" in body["detail"]["message"].lower()


def test_upload_succeeds_when_no_lock(client, monkeypatch):
    c, root = client
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: False)
    # Parche Windows: os.sync() solo existe en Unix; el handler lo invoca al
    # cerrar el lote. No afecta a la lógica que estamos validando aquí.
    import os as _os
    if not hasattr(_os, "sync"):
        monkeypatch.setattr(_os, "sync", lambda: None, raising=False)

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", _fake_pdf())],
    )
    assert r.status_code == 200
    body = r.json()
    assert "uploaded" in body
    # El fichero llegó al inbox.
    assert (root / "facturas" / "compras" / "factura.pdf").exists()
