"""Tests del endpoint GET /api/pipeline/batch."""

from __future__ import annotations

import json
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


def _write_pending_confirm(libros_root: Path, doc_ids: list[str]) -> None:
    p = libros_root / ".runtime" / ".pending_confirm.json"
    p.write_text(json.dumps({"doc_ids_lote": doc_ids, "status": "running"}))


def _write_status(libros_root: Path, **fields) -> None:
    p = libros_root / ".runtime" / "pipeline_status.json"
    payload = {"status": "running", "processed": 0, "total": 0}
    payload.update(fields)
    p.write_text(json.dumps(payload))


def _make_sidecar(
    libros_root: Path,
    folder_name: str,
    doc_id: str,
    last_status: str = "uploaded",
    file_origin: str | None = None,
    decision: str | None = None,
) -> Path:
    folder = libros_root / "asientos" / folder_name
    state_writer.init_uploaded(
        folder,
        doc_id=doc_id,
        file_origin=file_origin or f"facturas/compras/{doc_id}.pdf",
        sha256_file="a" * 64,
        size_bytes=100,
    )
    if last_status != "uploaded":
        ev: dict = {"status": last_status}
        if decision:
            ev["decision"] = decision
        state_writer.append(folder, ev)
    return folder


def test_batch_returns_in_flight_false_when_no_pending(client):
    c, _root = client
    r = c.get("/api/pipeline/batch")
    assert r.status_code == 200
    body = r.json()
    assert body["in_flight"] is False
    assert body["books"] == []


def test_batch_returns_in_flight_false_when_status_completed(client, libros_root):
    c, _root = client
    _write_pending_confirm(libros_root, ["doc_a"])
    _write_status(libros_root, status="completed")
    body = c.get("/api/pipeline/batch").json()
    assert body["in_flight"] is False


def test_batch_lists_files_grouped_by_libro_with_mixed_status(client, libros_root):
    c, root = client
    # Lote: 3 splits de compras + 1 suelta de ventas. Mezcla de estados.
    _make_sidecar(root, "compras_mix__1of3", "mix__1of3",
                  last_status="done", file_origin="facturas/compras/mix__1of3.pdf",
                  decision="auto")
    _make_sidecar(root, "compras_mix__2of3", "mix__2of3",
                  last_status="uploaded", file_origin="facturas/compras/mix__2of3.pdf")
    _make_sidecar(root, "compras_mix__3of3", "mix__3of3",
                  last_status="uploaded", file_origin="facturas/compras/mix__3of3.pdf")
    _make_sidecar(root, "ventas_sola", "sola",
                  last_status="uploaded", file_origin="facturas/ventas/sola.pdf")
    _write_pending_confirm(root, [
        "mix__1of3", "mix__2of3", "mix__3of3", "sola",
    ])
    _write_status(root, status="running", current_file="mix__2of3.pdf")

    body = c.get("/api/pipeline/batch").json()
    assert body["in_flight"] is True
    assert body["current_file"] == "mix__2of3.pdf"
    assert body["current_libro"] == "compras"

    # Dos secciones (compras + ventas), bienes no aparece (vacío).
    libros = {b["libro"] for b in body["books"]}
    assert libros == {"compras", "ventas"}

    compras = next(b for b in body["books"] if b["libro"] == "compras")
    by_doc = {f["doc_id"]: f for f in compras["files"]}

    assert by_doc["mix__1of3"]["status"] == "done"
    assert by_doc["mix__1of3"]["split_origin"] == "mix.pdf"
    assert by_doc["mix__1of3"]["split_index"] == 1
    assert by_doc["mix__1of3"]["split_total"] == 3
    assert by_doc["mix__1of3"]["decision"] == "auto"

    assert by_doc["mix__2of3"]["status"] == "processing"  # coincide con current_file
    assert by_doc["mix__3of3"]["status"] == "pending"

    ventas = next(b for b in body["books"] if b["libro"] == "ventas")
    assert len(ventas["files"]) == 1
    assert ventas["files"][0]["status"] == "pending"
    assert ventas["files"][0]["split_origin"] is None


def test_batch_handles_pending_doc_without_sidecar(client, libros_root):
    """Caso degenerado: doc_id en pending_confirm pero sin sidecar — usa fallback inbox."""
    c, root = client
    # Sin sidecar, pero con PDF en el inbox.
    (root / "facturas" / "compras" / "huerfano.pdf").write_bytes(b"%PDF")
    _write_pending_confirm(root, ["huerfano"])
    _write_status(root, status="running")
    body = c.get("/api/pipeline/batch").json()
    assert body["in_flight"] is True
    compras = next(b for b in body["books"] if b["libro"] == "compras")
    f = compras["files"][0]
    assert f["doc_id"] == "huerfano"
    assert f["filename"] == "huerfano.pdf"
    assert f["status"] == "pending"


def test_batch_marks_processing_when_current_file_matches(client, libros_root):
    c, root = client
    _make_sidecar(root, "compras_factura", "factura",
                  last_status="processing",
                  file_origin="facturas/compras/factura.pdf")
    _write_pending_confirm(root, ["factura"])
    _write_status(root, status="running", current_file="factura.pdf")
    body = c.get("/api/pipeline/batch").json()
    assert body["in_flight"] is True
    assert body["books"][0]["files"][0]["status"] == "processing"
    assert body["current_libro"] == "compras"
