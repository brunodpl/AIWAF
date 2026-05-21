"""Tests del filtrado por estado en GET /api/invoices.

Estados ``cancelled`` (soft-deleted) y ``split`` (PDF padre dividido) son
terminales no-actionables y NO deben aparecer en la cola del reviewer por
defecto. Si el historial quiere mostrarlos, debe pasar include_cancelled o
include_split como query params.
"""

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
    return TestClient(api_main.app), libros_root


def _seed_asiento(
    libros_root: Path, folder_name: str, doc_id: str, status_chain: list[str]
) -> Path:
    """Crea carpeta de asiento con sidecar siguiendo ``status_chain`` y un
    resultado_validacion.json mínimo para que ``/api/invoices`` no lo descarte
    como huérfano.

    El primer status SIEMPRE es ``uploaded`` (init_uploaded). Los siguientes
    se appendean en orden.
    """
    folder = libros_root / "asientos" / folder_name
    folder.mkdir(parents=True)
    state_writer.init_uploaded(folder, doc_id, f"facturas/compras/{doc_id}.pdf", "x" * 64, 100)
    for s in status_chain:
        if s == "split":
            state_writer.append(folder, {"status": "split", "split_into": [f"{doc_id}__1of1"]})
        else:
            state_writer.append(folder, {"status": s})
    (folder / "resultado_validacion.json").write_text(
        json.dumps({
            "documento_id": doc_id,
            "decision_global": "warn",
            "fecha_ensamblado": "2026-05-21T13:00:00Z",
            "libro": "20_COMPRAS_GASTOS",
            "campos": {
                "nif_entidad": {"valor_final": "B11111111"},
                "nombre_entidad": {"valor_final": f"Entidad {doc_id}"},
                "numero_factura": {"valor_final": f"F-{doc_id}"},
                "total_euros": {"valor_final": 100.0},
            },
        }),
        encoding="utf-8",
    )
    return folder


def test_cancelled_no_aparece_por_defecto(client):
    c, root = client
    _seed_asiento(root, "compras_activa", "activa", ["processing", "review"])
    _seed_asiento(root, "compras_borrada", "borrada", ["processing", "review", "cancelled"])

    r = c.get("/api/invoices")
    assert r.status_code == 200
    data = r.json()

    ids = {inv["id"] for inv in data["invoices"]}
    assert "activa" in ids
    assert "borrada" not in ids  # cancelled NO debe aparecer
    assert data["total"] == 1


def test_cancelled_aparece_con_include_cancelled(client):
    c, root = client
    _seed_asiento(root, "compras_activa", "activa", ["processing", "review"])
    _seed_asiento(root, "compras_borrada", "borrada", ["processing", "review", "cancelled"])

    r = c.get("/api/invoices?include_cancelled=true")
    assert r.status_code == 200
    data = r.json()
    ids = {inv["id"] for inv in data["invoices"]}
    assert "activa" in ids
    assert "borrada" in ids
    assert data["total"] == 2


def test_split_no_aparece_por_defecto(client):
    c, root = client
    _seed_asiento(root, "compras_pdfmulti", "pdfmulti", ["split"])
    _seed_asiento(root, "compras_normal", "normal", ["processing", "review"])

    r = c.get("/api/invoices")
    assert r.status_code == 200
    data = r.json()
    ids = {inv["id"] for inv in data["invoices"]}
    assert "normal" in ids
    assert "pdfmulti" not in ids  # split NO debe aparecer
    assert data["total"] == 1


def test_split_aparece_con_include_split(client):
    c, root = client
    _seed_asiento(root, "compras_pdfmulti", "pdfmulti", ["split"])
    _seed_asiento(root, "compras_normal", "normal", ["processing", "review"])

    r = c.get("/api/invoices?include_split=true")
    assert r.status_code == 200
    data = r.json()
    ids = {inv["id"] for inv in data["invoices"]}
    assert "normal" in ids
    assert "pdfmulti" in ids
    assert data["total"] == 2


def test_escenario_bug_bruno_replica_y_se_corrige(client):
    """Replica el estado que tenía Bruno cuando vio "1/7" con ghosts:
    facturas en review/blocked + cancelled (soft-deleted) + split + confirmed.
    Con el fix solo deben aparecer las actionables (review, blocked, done)."""
    c, root = client
    # Activas — deben aparecer
    _seed_asiento(root, "compras_3", "3", ["processing", "blocked"])
    _seed_asiento(root, "compras_8", "8", ["processing", "review"])
    _seed_asiento(root, "compras_9", "9", ["processing", "done"])
    # Ghosts — NO deben aparecer
    _seed_asiento(root, "compras_7", "7", ["processing", "review", "cancelled"])
    _seed_asiento(root, "compras_10", "10", ["processing", "review", "cancelled"])
    _seed_asiento(root, "compras_pdfpadre", "pdfpadre", ["split"])
    _seed_asiento(root, "compras_5", "5", ["processing", "done", "confirmed"])

    r = c.get("/api/invoices")
    assert r.status_code == 200
    data = r.json()
    ids = {inv["id"] for inv in data["invoices"]}

    assert ids == {"3", "8", "9"}  # solo activas
    assert data["total"] == 3


def test_cache_key_diferenciado_por_include_cancelled(client):
    """El cache_key debe incluir include_cancelled e include_split, sino una
    consulta posterior con flags distintos devolvería el cache anterior."""
    c, root = client
    _seed_asiento(root, "compras_borrada", "borrada", ["processing", "review", "cancelled"])

    # Primera consulta: oculta
    r1 = c.get("/api/invoices")
    assert r1.json()["total"] == 0

    # Segunda con include_cancelled — debe ser fresh
    r2 = c.get("/api/invoices?include_cancelled=true")
    assert r2.json()["total"] == 1
    assert r2.json()["invoices"][0]["id"] == "borrada"


# ---------------------------------------------------------------------------
# Contrato de /api/pipeline/batch con el frontend
# ---------------------------------------------------------------------------
#
# El reviewer del frontend filtra ``invoiceSummaries`` por los ``doc_id`` que
# devuelve ``/api/pipeline/batch`` para mostrar SOLO las facturas del lote en
# curso (evita ghosts de runs anteriores). Estos tests fijan el contrato:
# cuando hay ``.pending_confirm.json`` + status running, el endpoint devuelve
# ``in_flight=true`` y los ``doc_id`` esperados; cuando no, devuelve vacío.


def _seed_lote_activo(libros_root: Path, doc_ids: list[str]) -> None:
    """Crea .pending_confirm.json y pipeline_status.json=running para simular
    un run del pipeline a medias."""
    runtime = libros_root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / ".pending_confirm.json").write_text(
        json.dumps({"doc_ids_lote": doc_ids}),
        encoding="utf-8",
    )
    (runtime / "pipeline_status.json").write_text(
        json.dumps({"status": "running", "current_file": None}),
        encoding="utf-8",
    )


def test_pipeline_batch_devuelve_doc_ids_del_lote_activo(client):
    """Frontend depende de este contrato: cuando hay lote activo,
    /api/pipeline/batch devuelve in_flight=true y todos los doc_ids del lote
    distribuidos en books[].files[]. El reviewer construye el filtro
    `batchDocIds` a partir de aquí."""
    c, root = client
    # Tres facturas distribuidas en 2 libros, todas con sidecar.
    _seed_asiento(root, "compras_f1", "f1", ["processing", "review"])
    _seed_asiento(root, "compras_f2", "f2", ["processing", "blocked"])
    # Para ventas necesitamos un sidecar en su carpeta; reutilizamos el helper
    # pero forzando un libro distinto en el folder_name.
    folder_v = root / "asientos" / "ventas_f3"
    folder_v.mkdir(parents=True)
    state_writer.init_uploaded(folder_v, "f3", "facturas/ventas/f3.pdf", "x" * 64, 100)
    state_writer.append(folder_v, {"status": "processing"})
    state_writer.append(folder_v, {"status": "done"})

    _seed_lote_activo(root, ["f1", "f2", "f3"])

    r = c.get("/api/pipeline/batch")
    assert r.status_code == 200
    body = r.json()

    assert body["in_flight"] is True
    assert "books" in body
    doc_ids_devueltos = {
        f["doc_id"] for b in body["books"] for f in b.get("files", [])
    }
    assert doc_ids_devueltos == {"f1", "f2", "f3"}


def test_pipeline_batch_sin_lote_devuelve_in_flight_false(client):
    """Si no hay .pending_confirm.json el endpoint devuelve in_flight=false y
    lista vacía. El reviewer en ese caso mostrará "vuelve a Gestión" en lugar
    de listar fragmentos viejos."""
    c, _ = client
    r = c.get("/api/pipeline/batch")
    assert r.status_code == 200
    body = r.json()
    assert body["in_flight"] is False
    assert body["books"] == []


def test_pipeline_batch_no_incluye_facturas_de_runs_anteriores(client):
    """Aunque haya carpetas confirmed/cancelled/blocked en disco de runs
    anteriores, /api/pipeline/batch SOLO devuelve los doc_ids del lote actual
    (los listados en .pending_confirm.json). Eso garantiza que el filtro
    `batchDocIds` del reviewer ignora ghosts."""
    c, root = client
    # Restos de runs anteriores (no en el lote actual)
    _seed_asiento(root, "compras_old1", "old1", ["processing", "done", "confirmed"])
    _seed_asiento(root, "compras_old2", "old2", ["processing", "review", "cancelled"])
    _seed_asiento(root, "compras_old3", "old3", ["processing", "blocked"])
    # Factura del lote actual
    _seed_asiento(root, "compras_new", "new", ["processing", "review"])

    _seed_lote_activo(root, ["new"])

    r = c.get("/api/pipeline/batch")
    body = r.json()
    assert body["in_flight"] is True
    doc_ids_devueltos = {
        f["doc_id"] for b in body["books"] for f in b.get("files", [])
    }
    assert doc_ids_devueltos == {"new"}
