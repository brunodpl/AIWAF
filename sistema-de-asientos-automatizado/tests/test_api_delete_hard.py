"""Tests del hard-delete en DELETE /api/books/{book_id}/files/{filename}.

Política nueva: el operario espera que pulsar la 'X' borre el archivo
por completo, sin dejar rastro. Hard delete = ``shutil.rmtree`` de la
carpeta de asiento + ``unlink`` del PDF en el inbox. Única excepción:
si el sidecar está en ``confirmed`` (asiento ya exportado a Intermega),
devolvemos 409 para no desincronizar el libro contable cerrado.

Cubre:
- DELETE en ``confirmed``         → 409, PDF y carpeta intactos.
- DELETE en cualquier otro estado → hard delete total + caches limpias.
- DELETE sin sidecar (PDF huérfano) → hard delete del PDF.
- DELETE con pipeline bloqueado   → 409 (preserva comportamiento previo).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import state_writer
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
    cfg = SimpleNamespace(
        libros_base=str(libros_root),
        output_path=str(libros_root.parent / "data" / "output"),
        logs_path=str(libros_root / "logs"),
        sandbox_base_path=str(libros_root.parent / "sandbox"),
        inbox_path=lambda libro: str(libros_root / "facturas" / libro),
        asientos_path=lambda: str(libros_root / "asientos"),
        runtime_path=lambda: str(libros_root / ".runtime"),
        audit_path=lambda: str(libros_root / "logs" / "audit"),
    )
    monkeypatch.setattr(api_main, "settings", lambda: cfg)
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: False)
    import os as _os
    if not hasattr(_os, "sync"):
        monkeypatch.setattr(_os, "sync", lambda: None, raising=False)
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    api_main._clients_cache.clear()
    return TestClient(api_main.app), libros_root


def _create_file_with_sidecar(
    libros_root: Path, filename: str, status: str, content: bytes
) -> tuple[Path, Path]:
    """Crea PDF en inbox + sidecar con el estado pedido. Devuelve (pdf, folder).

    El sidecar incluye ``init_uploaded`` (estado base ``uploaded``) y, si el
    estado pedido es distinto, append events hasta llegar a él pasando por
    ``processing``. Para estados terminales (``done``, ``confirmed``) se
    encadenan los eventos correspondientes.
    """
    inbox = libros_root / "facturas" / "compras"
    pdf = inbox / filename
    pdf.write_bytes(content)

    doc_id = filename.rsplit(".", 1)[0]
    folder = libros_root / "asientos" / f"compras_{doc_id}"
    folder.mkdir()
    sha = hashlib.sha256(content).hexdigest()
    state_writer.init_uploaded(folder, doc_id, str(pdf), sha, len(content))
    if status == "uploaded":
        return pdf, folder
    state_writer.append(folder, {"status": "processing"})
    if status == "processing":
        return pdf, folder
    if status == "confirmed":
        state_writer.append(folder, {"status": "done"})
        state_writer.append(folder, {"status": "confirmed"})
    else:
        state_writer.append(folder, {"status": status})
    return pdf, folder


# ──────────────────────────────────────────────────────────────────────
# Test 2.1: confirmed → 409, todo intacto.
# ──────────────────────────────────────────────────────────────────────


def test_delete_en_confirmed_devuelve_409(client):
    c, root = client
    pdf, folder = _create_file_with_sidecar(
        root, "factura_cerrada.pdf", "confirmed", b"%PDF-1.6\n%EOF\n"
    )
    assert state_writer.current_status(folder) == "confirmed"

    r = c.delete("/api/books/gastos/files/factura_cerrada.pdf")
    assert r.status_code == 409
    assert "Intermega" in r.json()["detail"]

    # Nada cambió: PDF, carpeta y sidecar siguen ahí.
    assert pdf.exists()
    assert folder.exists()
    assert (folder / ".state.json").exists()
    assert state_writer.current_status(folder) == "confirmed"


# ──────────────────────────────────────────────────────────────────────
# Test 2.2: cualquier estado distinto de confirmed → hard delete.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "estado",
    [
        "uploaded",
        "pre_scan_failed",
        "review",
        "done",
        "blocked",
        "error",
        "cancelled",
        "retrying",
        "processing",
    ],
)
def test_delete_en_estado_no_confirmed_es_hard_delete(client, estado):
    c, root = client
    filename = f"factura_{estado}.pdf"
    pdf, folder = _create_file_with_sidecar(
        root, filename, estado, b"%PDF-1.4\n%EOF\n" + estado.encode()
    )

    # Poblamos caches para verificar que se invalidan tras el delete.
    api_main._invoices_cache["sentinel"] = "x"
    api_main._stats_cache["sentinel"] = "x"
    api_main._clients_cache["sentinel"] = "x"

    r = c.delete(f"/api/books/gastos/files/{filename}")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["deleted"] == filename
    assert body["asiento_deleted"] is True
    assert body["asiento_folder"] == folder.name

    # Hard delete: PDF y carpeta desaparecen sin dejar rastro.
    assert pdf.exists() is False
    assert folder.exists() is False

    # Caches invalidados.
    assert api_main._invoices_cache == {}
    assert api_main._stats_cache == {}
    assert api_main._clients_cache == {}


# ──────────────────────────────────────────────────────────────────────
# Test 2.3: PDF huérfano sin sidecar → hard delete sólo del PDF.
# ──────────────────────────────────────────────────────────────────────


def test_delete_sin_sidecar_es_hard_delete_solo_pdf(client):
    c, root = client
    inbox = root / "facturas" / "compras"
    pdf = inbox / "huerfano.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%EOF\n")

    r = c.delete("/api/books/gastos/files/huerfano.pdf")
    assert r.status_code == 200
    body = r.json()

    assert body["deleted"] == "huerfano.pdf"
    assert body["asiento_deleted"] is False
    assert body["asiento_folder"] is None
    assert not pdf.exists()


# ──────────────────────────────────────────────────────────────────────
# Test 2.4: pipeline bloqueado → 409 (preserva comportamiento previo).
# ──────────────────────────────────────────────────────────────────────


def test_delete_con_pipeline_bloqueado_devuelve_409(monkeypatch, client):
    c, root = client
    pdf, folder = _create_file_with_sidecar(
        root, "factura_lock.pdf", "review", b"%PDF-1.4\n%EOF\n"
    )

    # Forzamos lock activo (el fixture lo había desactivado).
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: True)

    r = c.delete("/api/books/gastos/files/factura_lock.pdf")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "pipeline" in detail.lower()

    # Nada cambió.
    assert pdf.exists()
    assert folder.exists()
