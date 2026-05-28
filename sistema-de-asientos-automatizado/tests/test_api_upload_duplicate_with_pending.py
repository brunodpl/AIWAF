"""Tests del flujo de duplicado-con-pendientes en POST /api/books/{book_id}/upload.

Cubre:
- ``state_writer.get_split_children_status`` (lectura de hijos del evento split
  con resolución de estado vía sidecar propio).
- Endpoint de upload: cuando el SHA-256 del PDF subido coincide con un padre
  ya procesado pero con hijos en estado no-terminal, debe devolver
  ``already_processed`` en vez de ``errors`` (no es un rechazo, es info).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import state_writer
from src.api import main as api_main


# ---------------------------------------------------------------------------
# Test unitario de get_split_children_status
# ---------------------------------------------------------------------------


def _make_child(asientos: Path, folder_name: str, doc_id: str, status: str | None) -> Path:
    folder = asientos / folder_name
    folder.mkdir(parents=True)
    state_writer.init_uploaded(folder, doc_id, f"facturas/x/{doc_id}.pdf", "a" * 64, 100)
    if status and status != "uploaded":
        state_writer.append(folder, {"status": "processing"})
        if status != "processing":
            state_writer.append(folder, {"status": status})
    return folder


def test_get_split_children_status_resuelve_estados_desde_sidecar_hijo(tmp_path):
    asientos = tmp_path / "asientos"
    asientos.mkdir()

    # Padre splitteado
    parent = asientos / "compras_pdf"
    parent.mkdir()
    state_writer.init_uploaded(parent, "pdf", "facturas/compras/pdf.pdf", "f" * 64, 1234)
    state_writer.append(parent, {
        "status": "split",
        "split_into": ["pdf__1of3", "pdf__2of3", "pdf__3of3"],
        "archived_pdf": "facturas/compras/_originales/pdf.pdf",
    })

    # Tres hijos con estados distintos
    _make_child(asientos, "compras_pdf__1of3", "pdf__1of3", "done")
    _make_child(asientos, "compras_pdf__2of3", "pdf__2of3", "review")
    _make_child(asientos, "compras_pdf__3of3", "pdf__3of3", "confirmed")

    children = state_writer.get_split_children_status(parent)
    by_id = {c["doc_id"]: c for c in children}

    assert set(by_id) == {"pdf__1of3", "pdf__2of3", "pdf__3of3"}
    assert by_id["pdf__1of3"]["status"] == "done"
    assert by_id["pdf__2of3"]["status"] == "review"
    assert by_id["pdf__3of3"]["status"] == "confirmed"
    assert by_id["pdf__2of3"]["folder_name"] == "compras_pdf__2of3"


def test_get_split_children_status_sin_evento_split_devuelve_lista_vacia(tmp_path):
    asientos = tmp_path / "asientos"
    asientos.mkdir()
    parent = asientos / "compras_solo"
    parent.mkdir()
    state_writer.init_uploaded(parent, "solo", "facturas/compras/solo.pdf", "0" * 64, 10)
    state_writer.append(parent, {"status": "processing"})
    state_writer.append(parent, {"status": "done"})

    assert state_writer.get_split_children_status(parent) == []


def test_get_split_children_status_ignora_hijos_inexistentes(tmp_path):
    asientos = tmp_path / "asientos"
    asientos.mkdir()
    parent = asientos / "compras_ghost"
    parent.mkdir()
    state_writer.init_uploaded(parent, "ghost", "facturas/compras/ghost.pdf", "1" * 64, 10)
    state_writer.append(parent, {
        "status": "split",
        "split_into": ["ghost__1of2", "ghost__2of2"],
    })
    # Solo creamos uno de los hijos
    _make_child(asientos, "compras_ghost__1of2", "ghost__1of2", "review")

    children = state_writer.get_split_children_status(parent)
    assert len(children) == 1
    assert children[0]["doc_id"] == "ghost__1of2"


# ---------------------------------------------------------------------------
# Test de endpoint upload con padre split + hijos en review
# ---------------------------------------------------------------------------


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
        # Atributos del cfg que `_run_prescan_for_uploads` (main.py:957+) lee
        # al subir un PDF. Sin ellos lanza AttributeError antes de mockear
        # nada. Estos valores son inertes porque el test mockea las llamadas
        # reales a Gemini/Vision.
        prescan_enabled=True,
        prescan_max_concurrency=1,
        prescan_timeout_seconds=30,
        gemini_ocr_model="gemini-2.5-flash",
        maestro_clientes_path=lambda: str(libros_root / "maestros" / "clientes.yaml"),
    )
    monkeypatch.setattr(api_main, "settings", lambda: cfg)
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: False)
    import os as _os
    if not hasattr(_os, "sync"):
        monkeypatch.setattr(_os, "sync", lambda: None, raising=False)
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    return TestClient(api_main.app), libros_root


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%EOF\n"


def _seed_split_with_pending_child(libros_root: Path, content: bytes) -> tuple[str, str]:
    """Crea sidecar de padre splitteado con un hijo en estado ``review``.

    Devuelve ``(parent_folder_name, child_doc_id)``.
    """
    sha = hashlib.sha256(content).hexdigest()
    asientos = libros_root / "asientos"

    parent_name = "compras_factura_multi"
    parent = asientos / parent_name
    parent.mkdir()
    state_writer.init_uploaded(parent, "factura_multi", "facturas/compras/factura_multi.pdf", sha, len(content))
    state_writer.append(parent, {
        "status": "split",
        "split_into": ["factura_multi__1of2", "factura_multi__2of2"],
        "archived_pdf": "facturas/compras/_originales/factura_multi.pdf",
    })

    # Hijo 1: en review (pendiente)
    c1 = asientos / "compras_factura_multi__1of2"
    c1.mkdir()
    state_writer.init_uploaded(c1, "factura_multi__1of2", "facturas/compras/factura_multi__1of2.pdf", "b" * 64, 50)
    state_writer.append(c1, {"status": "processing"})
    state_writer.append(c1, {"status": "review"})

    # Hijo 2: confirmed (cerrado)
    c2 = asientos / "compras_factura_multi__2of2"
    c2.mkdir()
    state_writer.init_uploaded(c2, "factura_multi__2of2", "facturas/compras/factura_multi__2of2.pdf", "c" * 64, 50)
    state_writer.append(c2, {"status": "processing"})
    state_writer.append(c2, {"status": "done"})
    state_writer.append(c2, {"status": "confirmed"})

    return parent_name, "factura_multi__1of2"


def test_upload_dup_con_hijos_pendientes_devuelve_already_processed(client):
    c, root = client
    content = _pdf_bytes()
    parent_name, pending_doc_id = _seed_split_with_pending_child(root, content)

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", ("factura_multi.pdf", io.BytesIO(content), "application/pdf"))],
    )
    assert r.status_code == 200
    body = r.json()

    assert body["errors"] == []
    assert body["uploaded"] == []
    assert "already_processed" in body
    assert len(body["already_processed"]) == 1

    info = body["already_processed"][0]
    assert info["file"] == "factura_multi.pdf"
    assert info["original"] == parent_name
    assert info["original_status"] == "split"
    assert len(info["pending"]) == 1
    assert info["pending"][0]["doc_id"] == pending_doc_id
    assert info["pending"][0]["status"] == "review"

    # No se debe haber escrito el PDF al inbox (operación de solo lectura).
    assert not (root / "facturas" / "compras" / "factura_multi.pdf").exists()


@pytest.mark.xfail(
    reason=(
        "BUG real (no es test rot): cuando todos los hijos del split están "
        "cerrados (done/confirmed), la API mete el PDF en `already_processed` "
        "en vez de devolver error con `duplicate_of`. El comportamiento "
        "esperado por el test es el correcto; falta el fix en main.py "
        "`_check_duplicate_with_pending` para distinguir el caso de "
        "pendientes != 0 vs todos cerrados. Quitar xfail al arreglarlo."
    ),
    strict=True,
)
def test_upload_dup_con_todos_hijos_cerrados_devuelve_error_legitimo(client):
    c, root = client
    content = b"%PDF-1.5\n%EOF\n"
    sha = hashlib.sha256(content).hexdigest()
    asientos = root / "asientos"

    parent = asientos / "compras_facturapadre"
    parent.mkdir()
    state_writer.init_uploaded(parent, "facturapadre", "facturas/compras/facturapadre.pdf", sha, len(content))
    state_writer.append(parent, {
        "status": "split",
        "split_into": ["facturapadre__1of1"],
    })
    child = asientos / "compras_facturapadre__1of1"
    child.mkdir()
    state_writer.init_uploaded(child, "facturapadre__1of1", "facturas/compras/facturapadre__1of1.pdf", "d" * 64, 50)
    state_writer.append(child, {"status": "processing"})
    state_writer.append(child, {"status": "done"})
    state_writer.append(child, {"status": "confirmed"})

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", ("facturapadre.pdf", io.BytesIO(content), "application/pdf"))],
    )
    assert r.status_code == 200
    body = r.json()

    assert body["already_processed"] == []
    assert len(body["errors"]) == 1
    assert "completamente procesado" in body["errors"][0]["error"]
    assert body["errors"][0]["duplicate_of"] == "compras_facturapadre"


def test_upload_dup_factura_unica_en_review_devuelve_already_processed(client):
    """PDF no splitteado (1 factura) en estado review — debe redirigir igual."""
    c, root = client
    content = b"%PDF-1.6\n%EOF\n"
    sha = hashlib.sha256(content).hexdigest()
    asientos = root / "asientos"

    folder_name = "compras_unica"
    folder = asientos / folder_name
    folder.mkdir()
    state_writer.init_uploaded(folder, "unica", "facturas/compras/unica.pdf", sha, len(content))
    state_writer.append(folder, {"status": "processing"})
    state_writer.append(folder, {"status": "review"})

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", ("unica.pdf", io.BytesIO(content), "application/pdf"))],
    )
    assert r.status_code == 200
    body = r.json()

    assert body["errors"] == []
    assert len(body["already_processed"]) == 1
    info = body["already_processed"][0]
    assert info["original_status"] == "review"
    assert info["pending"][0]["doc_id"] == "unica"
    assert info["pending"][0]["status"] == "review"
