"""Lifecycle: factura procesada → rechazo (review, count=1) → 2º rechazo (count=2).

El backend solo cuenta — el hard delete tras el segundo reject lo dispara el
cliente vía DELETE. Aquí verificamos que:

- Tras el primer POST /api/invoices/{doc_id}/action {"action":"reject"} el
  sidecar transiciona a ``review`` y ``rejection_count`` expuesto en
  ``/api/invoices`` vale 1.
- Tras un segundo reject el contador sube a 2 sin que el backend borre nada.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import state_writer
from src.api import main as api_main


# --- Fixtures -------------------------------------------------------------


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
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    return TestClient(api_main.app)


@pytest.fixture
def processed_invoice(libros_root: Path):
    """Crea una factura en estado ``review`` con resultado_validacion mínimo."""
    doc_id = "factura_reject_lc"
    folder = libros_root / "asientos" / f"compras_{doc_id}"
    folder.mkdir(parents=True)
    state_writer.init(
        folder,
        doc_id=doc_id,
        file_origin=f"libros/facturas/compras/{doc_id}.pdf",
    )
    state_writer.append(folder, {"status": "review", "decision": "warn"})
    (folder / "resultado_validacion.json").write_text(
        json.dumps({
            "documento_id": doc_id,
            "decision_global": "warn",
            "fecha_ensamblado": "2026-05-08T10:00:00Z",
            "libro": "20_COMPRAS_GASTOS",
            "campos": {
                "nif_entidad": {"valor_final": "B12345678"},
                "nombre_entidad": {"valor_final": "Proveedor SL"},
                "numero_factura": {"valor_final": "F-2026-001"},
                "total_euros": {"valor_final": 121.0},
                "lineas_fiscales": {"valor_final": []},
            },
        }),
        encoding="utf-8",
    )
    return SimpleNamespace(doc_id=doc_id, folder=folder)


# --- Tests ----------------------------------------------------------------


def test_first_reject_transitions_to_review_and_increments_count(client, processed_invoice):
    doc_id = processed_invoice.doc_id

    r = client.post(
        f"/api/invoices/{doc_id}/action",
        json={"action": "reject", "document_id": doc_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_status"] == "review"

    # El último evento es un reject de usuario.
    state = state_writer.read(processed_invoice.folder)
    last = state["events"][-1]
    assert last["status"] == "review"
    assert last["actor"] == "user"
    assert last["action"] == "reject"

    assert state_writer.current_status(processed_invoice.folder) == "review"

    invoices = client.get("/api/invoices").json()["invoices"]
    inv = next(i for i in invoices if i["id"] == doc_id)
    assert inv["rejection_count"] == 1


def test_second_reject_increments_counter_without_deleting(client, processed_invoice):
    """Backend solo cuenta — el hard delete lo hace el cliente vía DELETE."""
    doc_id = processed_invoice.doc_id

    r1 = client.post(
        f"/api/invoices/{doc_id}/action",
        json={"action": "reject", "document_id": doc_id},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        f"/api/invoices/{doc_id}/action",
        json={"action": "reject", "document_id": doc_id},
    )
    assert r2.status_code == 200, r2.text

    invoices = client.get("/api/invoices").json()["invoices"]
    inv = next(i for i in invoices if i["id"] == doc_id)
    assert inv["rejection_count"] == 2

    # La carpeta sigue existiendo: el backend no hace auto-delete.
    assert processed_invoice.folder.exists()
    assert (processed_invoice.folder / ".state.json").exists()


def test_rejection_count_zero_for_invoice_never_rejected(client, libros_root):
    """Factura en ``done`` sin reject alguno debe exponer rejection_count=0."""
    doc_id = "factura_sin_reject"
    folder = libros_root / "asientos" / f"compras_{doc_id}"
    folder.mkdir(parents=True)
    state_writer.init(
        folder,
        doc_id=doc_id,
        file_origin=f"libros/facturas/compras/{doc_id}.pdf",
    )
    state_writer.append(folder, {"status": "done", "decision": "auto"})
    (folder / "resultado_validacion.json").write_text(
        json.dumps({
            "documento_id": doc_id,
            "decision_global": "auto",
            "fecha_ensamblado": "2026-05-08T10:00:00Z",
            "libro": "20_COMPRAS_GASTOS",
            "campos": {
                "nif_entidad": {"valor_final": "B11111111"},
                "nombre_entidad": {"valor_final": "X SL"},
                "numero_factura": {"valor_final": "F-2026-002"},
                "total_euros": {"valor_final": 50.0},
                "lineas_fiscales": {"valor_final": []},
            },
        }),
        encoding="utf-8",
    )

    invoices = client.get("/api/invoices").json()["invoices"]
    inv = next(i for i in invoices if i["id"] == doc_id)
    assert inv["rejection_count"] == 0
