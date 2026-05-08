"""Tests de integración del API — trazabilidad 2.0.

Cubre los flujos transformados:
- /api/invoices (lista basada en .state.json + folder_name)
- /api/invoices/{doc_id} (detalle + status + events)
- /api/invoices/{doc_id}/action (StateWriter, no move, no action_log.json)
- /api/books (inboxes permanentes, status por archivo)
- /api/books/{book_id}/upload (destino directo a libros/facturas/{libro}/)
- /api/pipeline/reset (borra asientos, no toca PDFs)
- /api/stats (deriva acciones humanas de eventos del sidecar)
- helpers _libro_from_folder / find_invoice_file

Las pruebas mockean ``settings()`` con un objeto que apunta a un tmp_path,
evitando dependencia de `.env`.
"""

from __future__ import annotations

import io
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
    """Crea estructura libros/ vacía."""
    root = tmp_path / "libros"
    (root / "facturas" / "compras").mkdir(parents=True)
    (root / "facturas" / "ventas").mkdir(parents=True)
    (root / "facturas" / "bienes").mkdir(parents=True)
    (root / "asientos").mkdir(parents=True)
    (root / "logs" / "audit").mkdir(parents=True)
    (root / ".runtime").mkdir(parents=True)
    return root


@pytest.fixture
def fake_settings(libros_root: Path):
    """Reemplaza ``settings()`` por un namespace que apunta al tmp libros/."""
    base = str(libros_root)
    cfg = SimpleNamespace(
        libros_base=base,
        output_path=str(libros_root.parent / "data" / "output"),
        logs_path=str(libros_root / "logs"),
        sandbox_base_path=str(libros_root.parent / "horeca_sandbox"),
        # Helpers nuevos:
        inbox_path=lambda libro: str(libros_root / "facturas" / libro),
        asientos_path=lambda: str(libros_root / "asientos"),
        runtime_path=lambda: str(libros_root / ".runtime"),
        audit_path=lambda: str(libros_root / "logs" / "audit"),
    )
    return cfg


@pytest.fixture
def client(monkeypatch, fake_settings):
    monkeypatch.setattr(api_main, "settings", lambda: fake_settings)
    # Limpiar cachés in-memory entre tests para aislamiento.
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    return TestClient(api_main.app)


def _make_asiento(
    libros_root: Path,
    folder_name: str,
    doc_id: str,
    decision: str = "auto",
    events_extra: list[dict] | None = None,
) -> Path:
    """Crea una carpeta de asiento con sidecar + resultado_validacion mínimo."""
    folder = libros_root / "asientos" / folder_name
    folder.mkdir(parents=True)
    state_writer.init(folder, doc_id=doc_id, file_origin=f"libros/facturas/compras/{doc_id}.pdf")
    if decision != "processing":
        state_writer.append(folder, {"status": "done" if decision == "auto" else "review", "decision": decision})
    for ev in events_extra or []:
        state_writer.append(folder, ev)
    (folder / "resultado_validacion.json").write_text(
        json.dumps({
            "documento_id": doc_id,
            "decision_global": decision,
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
    return folder


# --- helpers ---------------------------------------------------------------


def test_libro_from_folder_recognizes_prefix():
    assert api_main._libro_from_folder("compras_factura_xxx") == "compras"
    assert api_main._libro_from_folder("ventas_2026-04-15_A1_F1") == "ventas"
    assert api_main._libro_from_folder("bienes_x") == "bienes"
    assert api_main._libro_from_folder("desconocido_x") is None


def test_book_id_from_folder():
    assert api_main._book_id_from_folder("compras_x") == "gastos"
    assert api_main._book_id_from_folder("ventas_x") == "ingresos"
    assert api_main._book_id_from_folder("bienes_x") == "bienes"
    assert api_main._book_id_from_folder("xx_x") is None


# --- /api/invoices ---------------------------------------------------------


def test_list_invoices_empty(client):
    r = client.get("/api/invoices")
    assert r.status_code == 200
    assert r.json() == {"invoices": [], "total": 0}


def test_list_invoices_returns_status_and_folder_name(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="auto")
    _make_asiento(libros_root, "compras_factura_b", "factura_b", decision="warn")

    r = client.get("/api/invoices")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    inv_a = next(i for i in data["invoices"] if i["id"] == "factura_a")
    assert inv_a["folder_name"] == "compras_factura_a"
    assert inv_a["libro"] == "compras"
    assert inv_a["status"] == "done"
    assert inv_a["decision_global"] == "auto"
    assert inv_a["nif_entidad"] == "B12345678"


def test_list_invoices_skips_folders_without_sidecar(client, libros_root):
    # Carpeta huérfana sin .state.json → ignorada
    huerfana = libros_root / "asientos" / "compras_huerfana"
    huerfana.mkdir()
    _make_asiento(libros_root, "compras_buena", "factura_buena", decision="auto")

    data = client.get("/api/invoices").json()
    assert data["total"] == 1
    assert data["invoices"][0]["id"] == "factura_buena"


# --- /api/invoices/{doc_id} ------------------------------------------------


def test_get_invoice_detail_includes_events_and_status(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="warn")

    r = client.get("/api/invoices/factura_a")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "factura_a"
    assert body["folder_name"] == "compras_factura_a"
    assert body["libro"] == "compras"
    assert body["status"] == "review"
    assert body["decision_global"] == "warn"
    # Lifecycle: processing → review (mínimo 2 eventos)
    assert len(body["events"]) >= 2
    assert body["events"][0]["status"] == "processing"


def test_get_invoice_returns_404_when_doc_id_unknown(client):
    r = client.get("/api/invoices/desconocido")
    assert r.status_code == 404


def test_get_invoice_rejects_path_traversal(client):
    # `..` literal en el path-param → validate_doc_id devuelve 400.
    r = client.get("/api/invoices/bad..traversal")
    assert r.status_code == 400


# --- /api/invoices/{doc_id}/action -----------------------------------------


def test_action_approve_appends_done_event_and_does_not_move_pdf(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="warn")
    pdf = libros_root / "facturas" / "compras" / "factura_a.pdf"
    pdf.write_bytes(b"%PDF dummy")

    r = client.post(
        "/api/invoices/factura_a/action",
        json={"action": "approve", "document_id": "factura_a", "notes": "OK"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["new_status"] == "done"
    assert body["folder_name"] == "compras_factura_a"

    # PDF original NO se ha movido
    assert pdf.exists()

    # Sidecar tiene el evento de aprobación
    state = state_writer.read(libros_root / "asientos" / "compras_factura_a")
    last = state["events"][-1]
    assert last["status"] == "done"
    assert last["action"] == "approve"
    assert last["actor"] == "user"
    assert last["notes"] == "OK"

    # No se ha creado action_log.json
    assert not (libros_root / "asientos" / "compras_factura_a" / "action_log.json").exists()


def test_action_reject_keeps_status_review(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="warn")

    r = client.post(
        "/api/invoices/factura_a/action",
        json={"action": "reject", "document_id": "factura_a"},
    )
    assert r.status_code == 200
    assert r.json()["new_status"] == "review"

    state = state_writer.read(libros_root / "asientos" / "compras_factura_a")
    assert state["events"][-1]["action"] == "reject"


def test_action_invalid_returns_400(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="auto")
    r = client.post(
        "/api/invoices/factura_a/action",
        json={"action": "invent", "document_id": "factura_a"},
    )
    assert r.status_code == 400


# --- /api/books ------------------------------------------------------------


def test_list_books_returns_inboxes_with_files(client, libros_root):
    (libros_root / "facturas" / "compras" / "f1.pdf").write_bytes(b"x")
    (libros_root / "facturas" / "ventas" / "f2.pdf").write_bytes(b"x")

    r = client.get("/api/books")
    assert r.status_code == 200
    books = {b["id"]: b for b in r.json()["books"]}
    assert books["gastos"]["libro_short"] == "compras"
    assert len(books["gastos"]["files"]) == 1
    assert books["gastos"]["files"][0]["name"] == "f1.pdf"
    assert len(books["ingresos"]["files"]) == 1
    assert books["bienes"]["files"] == []


def test_list_books_attaches_status_when_processed(client, libros_root):
    """Si una factura ya tiene asiento, el listado expone su status actual."""
    (libros_root / "facturas" / "compras" / "factura_a.pdf").write_bytes(b"x")
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="auto")

    r = client.get("/api/books")
    files = next(b for b in r.json()["books"] if b["id"] == "gastos")["files"]
    assert files[0]["status"] == "done"
    assert files[0]["folder_name"] == "compras_factura_a"


def test_upload_writes_directly_to_inbox(client, libros_root):
    r = client.post(
        "/api/books/gastos/upload",
        files=[("files", ("nueva.pdf", io.BytesIO(b"%PDF data"), "application/pdf"))],
    )
    assert r.status_code == 200
    assert r.json()["uploaded"] == ["nueva.pdf"]
    assert (libros_root / "facturas" / "compras" / "nueva.pdf").exists()


def test_upload_invalid_book_id(client):
    r = client.post(
        "/api/books/no_existe/upload",
        files=[("files", ("a.pdf", io.BytesIO(b"x"), "application/pdf"))],
    )
    assert r.status_code == 400


def test_delete_book_file_removes_from_inbox(client, libros_root):
    target = libros_root / "facturas" / "compras" / "borrame.pdf"
    target.write_bytes(b"x")

    r = client.delete("/api/books/gastos/files/borrame.pdf")
    assert r.status_code == 200
    assert not target.exists()


# --- /api/pipeline/reset ---------------------------------------------------


def test_reset_deletes_asientos_but_not_pdfs(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="warn")
    pdf = libros_root / "facturas" / "compras" / "factura_a.pdf"
    pdf.write_bytes(b"%PDF")
    # Status file de un run anterior (residuo); NO un lock activo.
    status_path = libros_root / ".runtime" / "pipeline_status.json"
    status_path.write_text(json.dumps({"status": "completed"}))

    r = client.post("/api/pipeline/reset")
    assert r.status_code == 200
    assert r.json()["asientos_deleted"] == 1

    assert not (libros_root / "asientos" / "compras_factura_a").exists()
    assert pdf.exists(), "El PDF NO debe moverse ni borrarse en reset"
    assert not status_path.exists()  # runtime limpio


def test_reset_writes_audit_event(client, libros_root):
    _make_asiento(libros_root, "compras_a", "a", decision="auto")
    r = client.post("/api/pipeline/reset")
    assert r.status_code == 200

    audit_dir = libros_root / "logs" / "audit"
    reset_logs = list(audit_dir.glob("reset_*.jsonl"))
    assert len(reset_logs) == 1
    record = json.loads(reset_logs[0].read_text(encoding="utf-8").strip())
    assert record["event"] == "reset_batch"
    assert record["asientos_deleted"] == 1


def test_reset_blocked_when_pipeline_locked(client, libros_root, monkeypatch):
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: True)
    r = client.post("/api/pipeline/reset")
    assert r.status_code == 409


# --- /api/stats ------------------------------------------------------------


def test_stats_aggregates_decisions_and_human_actions(client, libros_root):
    _make_asiento(libros_root, "compras_a", "a", decision="auto")
    _make_asiento(libros_root, "compras_b", "b", decision="warn")
    folder_b = libros_root / "asientos" / "compras_b"
    state_writer.append(folder_b, {"status": "done", "actor": "user", "action": "approve"})

    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["by_decision"]["auto"] == 1
    assert body["by_decision"]["warn"] == 1
    assert body["by_user_action"]["approved"] == 1
    assert body["by_user_action"]["rejected"] == 0


# --- find_invoice_file ----------------------------------------------------


def test_find_invoice_file_uses_state_file_origin(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="auto")
    pdf = libros_root / "facturas" / "compras" / "factura_a.pdf"
    pdf.write_bytes(b"%PDF")

    # Cambiar cwd temporalmente: file_origin guarda ruta relativa al cwd
    import os
    cwd = os.getcwd()
    os.chdir(libros_root.parent)
    try:
        result = api_main.find_invoice_file("factura_a")
    finally:
        os.chdir(cwd)
    assert result is not None
    assert result[1] == "factura_a.pdf"


def test_find_invoice_file_returns_none_when_pdf_missing(client, libros_root):
    _make_asiento(libros_root, "compras_factura_a", "factura_a", decision="auto")
    # No creamos el PDF
    result = api_main.find_invoice_file("factura_a")
    assert result is None
