"""Tests del pre-scan síncrono en POST /api/books/{book_id}/upload.

Cubre:
- Upload de PDF single-page → ``pre_scan.status="single"``.
- Upload de PDF multi-factura (mock de split_single_file) → ``pre_scan.status="split"``
  con ``children`` poblado.
- Upload con splitter que falla → ``pre_scan.status="failed"`` + sidecar en
  estado ``pre_scan_failed``.
- Upload de imagen → ``pre_scan.status="skipped_non_pdf"``.
- Re-upload de PDF en ``pre_scan_failed`` → permite retry (no se marca dup).
- Endpoints /retry-prescan y /override-prescan.
- ``prescan_enabled=false`` → comportamiento legacy (sidecar uploaded sin pre_scan).
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import state_writer
from src.api import main as api_main
from src.phase1_splitter.main import SplitOutcome


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────


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
def cfg(libros_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        libros_base=str(libros_root),
        output_path=str(libros_root.parent / "data" / "output"),
        logs_path=str(libros_root / "logs"),
        sandbox_base_path=str(libros_root.parent / "sandbox"),
        gemini_ocr_model="gemini-2.5-flash",
        prescan_enabled=True,
        prescan_max_attempts=2,
        prescan_max_concurrency=2,
        prescan_timeout_seconds=30,
        inbox_path=lambda libro: str(libros_root / "facturas" / libro),
        asientos_path=lambda: str(libros_root / "asientos"),
        runtime_path=lambda: str(libros_root / ".runtime"),
        audit_path=lambda: str(libros_root / "logs" / "audit"),
    )


@pytest.fixture
def client(monkeypatch, cfg, libros_root):
    monkeypatch.setattr(api_main, "settings", lambda: cfg)
    monkeypatch.setattr(api_main, "is_pipeline_locked", lambda: False)
    # Stub del Gemini client: cualquier instancia no-None vale, no se usa
    # directamente porque split_single_file está mockeado.
    api_main.app.state.gemini_client = object()
    api_main.app.state._gemini_init_lock = None  # se inicializa en runtime
    import os as _os
    if not hasattr(_os, "sync"):
        monkeypatch.setattr(_os, "sync", lambda: None, raising=False)
    api_main._invoices_cache.clear()
    api_main._stats_cache.clear()
    return TestClient(api_main.app), libros_root


def _pdf_bytes(seed: int = 1) -> bytes:
    """PDF mínimo válido sintáctico — contenido cambia con `seed` para
    generar sha256 distintos.
    """
    return f"%PDF-1.{seed}\n%EOF\n".encode("ascii")


def _upload(client, filename: str, content: bytes, book="gastos"):
    return client.post(
        f"/api/books/{book}/upload",
        files=[("files", (filename, io.BytesIO(content), "application/pdf"))],
    )


# ──────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────


def test_upload_single_pdf_returns_pre_scan_single(monkeypatch, client):
    """PDF de 1 factura: pre_scan.status == 'single', detected_invoices=1."""
    c, root = client

    def fake_split(pdf_path, *, client=None, model=None, max_attempts=None):
        return SplitOutcome(
            source=str(pdf_path), status="single_page", n_pages=1, n_facturas=1,
            outputs=[str(pdf_path)],
        )
    monkeypatch.setattr(api_main, "split_single_file", fake_split)

    r = _upload(c, "una.pdf", _pdf_bytes(1))
    assert r.status_code == 200
    body = r.json()

    assert body["errors"] == []
    assert body["uploaded"] == ["una.pdf"]
    assert body["detected_summary"]["total_invoices"] == 1
    assert body["detected_summary"]["files_ok"] == 1
    assert body["detected_summary"]["files_blocked"] == 0

    [file_info] = body["files"]
    assert file_info["status"] == "uploaded"
    assert file_info["pre_scan"]["status"] == "single"
    assert file_info["pre_scan"]["detected_invoices"] == 1
    assert file_info["pre_scan"]["children"] == []
    assert file_info["pre_scan"]["error"] is None


def test_upload_multi_factura_pdf_returns_split_children(monkeypatch, client):
    """PDF con 3 facturas: pre_scan.status == 'split', children con 3 entradas."""
    c, root = client
    asientos = root / "asientos"

    def fake_split(pdf_path, *, client=None, model=None, max_attempts=None):
        # Simulamos lo que haría _apply_split: crear sidecars hijos en
        # state_writer y devolver SplitOutcome con status="split".
        pdf = Path(pdf_path)
        parent_folder = asientos / f"compras_{pdf.stem}"
        state_writer.append(parent_folder, {
            "status": "split",
            "split_into": [f"{pdf.stem}__1of3", f"{pdf.stem}__2of3", f"{pdf.stem}__3of3"],
            "archived_pdf": f"_originales/{pdf.name}",
        })
        for i in range(1, 4):
            child_doc_id = f"{pdf.stem}__{i}of3"
            child_folder = asientos / f"compras_{child_doc_id}"
            state_writer.init_uploaded(
                child_folder, child_doc_id, f"{child_doc_id}.pdf",
                f"sha-{i}" + "0" * 60, 100,
            )
        return SplitOutcome(
            source=str(pdf), status="split", n_pages=3, n_facturas=3,
            outputs=[f"{pdf.stem}__{i}of3.pdf" for i in range(1, 4)],
        )

    monkeypatch.setattr(api_main, "split_single_file", fake_split)

    r = _upload(c, "lote.pdf", _pdf_bytes(2))
    assert r.status_code == 200
    body = r.json()

    assert body["detected_summary"]["total_invoices"] == 3
    assert body["detected_summary"]["files_ok"] == 1

    [file_info] = body["files"]
    pre = file_info["pre_scan"]
    assert pre["status"] == "split"
    assert pre["detected_invoices"] == 3
    assert len(pre["children"]) == 3
    assert {c["doc_id"] for c in pre["children"]} == {
        "lote__1of3", "lote__2of3", "lote__3of3",
    }


def test_upload_with_splitter_failure_marks_pre_scan_failed(monkeypatch, client):
    """split_single_file devuelve error → sidecar termina en pre_scan_failed."""
    c, root = client

    def failing_split(pdf_path, *, client=None, model=None, max_attempts=None):
        return SplitOutcome(
            source=str(pdf_path), status="error", n_pages=3,
            error="gemini: timeout after 30s",
        )
    monkeypatch.setattr(api_main, "split_single_file", failing_split)

    r = _upload(c, "boom.pdf", _pdf_bytes(3))
    assert r.status_code == 200
    body = r.json()

    assert body["detected_summary"]["files_blocked"] == 1
    assert body["detected_summary"]["total_invoices"] == 0

    [file_info] = body["files"]
    pre = file_info["pre_scan"]
    assert pre["status"] == "failed"
    assert pre["error"]["kind"] == "gemini_pre_scan_failed"
    assert "timeout" in pre["error"]["message"]

    # Sidecar terminó en pre_scan_failed.
    asiento = root / "asientos" / "compras_boom"
    assert state_writer.current_status(asiento) == "pre_scan_failed"


def test_upload_image_skips_prescan(monkeypatch, client):
    """Imagen (PNG): no se llama a Gemini, pre_scan.status == 'skipped_non_pdf'."""
    c, root = client

    called = {"n": 0}
    def fake_split(*args, **kwargs):
        called["n"] += 1
        return SplitOutcome(source="x", status="single_page", n_pages=1, n_facturas=1)
    monkeypatch.setattr(api_main, "split_single_file", fake_split)

    r = c.post(
        "/api/books/gastos/upload",
        files=[("files", ("scan.png", io.BytesIO(b"\x89PNG\r\n\x1a\nstub"), "image/png"))],
    )
    assert r.status_code == 200
    body = r.json()
    assert called["n"] == 0
    [file_info] = body["files"]
    assert file_info["pre_scan"]["status"] == "skipped_non_pdf"
    assert body["detected_summary"]["files_skipped"] == 1
    assert body["detected_summary"]["total_invoices"] == 1


def test_upload_prescan_disabled_legacy_mode(monkeypatch, cfg, client):
    """``prescan_enabled=False`` → no se llama al splitter, pre_scan.status == 'skipped_disabled'."""
    c, root = client
    cfg.prescan_enabled = False

    called = {"n": 0}
    def fake_split(*args, **kwargs):
        called["n"] += 1
        return SplitOutcome(source="x", status="single_page")
    monkeypatch.setattr(api_main, "split_single_file", fake_split)

    r = _upload(c, "legacy.pdf", _pdf_bytes(4))
    assert r.status_code == 200
    body = r.json()
    assert called["n"] == 0
    assert body["files"][0]["pre_scan"]["status"] == "skipped_disabled"
    assert body["detected_summary"]["files_skipped"] == 1


def test_reupload_of_pre_scan_failed_pdf_is_allowed(monkeypatch, client):
    """Tras un pre_scan_failed, re-subir el mismo PDF no es duplicado — debe
    correr el pre-scan otra vez (fresh attempt).
    """
    c, root = client
    content = _pdf_bytes(5)

    # 1ª subida → falla pre-scan
    monkeypatch.setattr(api_main, "split_single_file", lambda *a, **kw: SplitOutcome(
        source="x", status="error", n_pages=2, error="gemini: 503",
    ))
    r1 = _upload(c, "retry.pdf", content)
    assert r1.status_code == 200
    assert r1.json()["detected_summary"]["files_blocked"] == 1

    # 2ª subida del mismo sha256 → ahora pre-scan tiene éxito.
    asiento = root / "asientos" / "compras_retry"
    monkeypatch.setattr(api_main, "split_single_file", lambda pdf_path, **kw: SplitOutcome(
        source=str(pdf_path), status="single_page", n_pages=1, n_facturas=1,
    ))
    r2 = _upload(c, "retry.pdf", content)
    assert r2.status_code == 200
    body2 = r2.json()
    # No debe aparecer como already_processed ni como error duplicate:
    # debe ser un upload nuevo (con nombre nuevo por colisión _ts) que
    # corre pre-scan fresh.
    assert body2["already_processed"] == []
    assert not any("duplicate" in (e.get("error") or "") for e in body2["errors"])


# ──────────────────────────────────────────────────────────
# /retry-prescan endpoint
# ──────────────────────────────────────────────────────────


def _seed_pre_scan_failed(libros_root: Path, doc_id: str = "stuck", book: str = "compras"):
    """Coloca un PDF en pre_scan_failed con su sidecar y su PDF en inbox."""
    inbox = libros_root / "facturas" / book
    pdf = inbox / f"{doc_id}.pdf"
    pdf.write_bytes(_pdf_bytes(7))

    folder = libros_root / "asientos" / f"{book}_{doc_id}"
    folder.mkdir(parents=True)
    state_writer.init_uploaded(folder, doc_id, str(pdf), "a" * 64, pdf.stat().st_size)
    state_writer.append(folder, {
        "status": "pre_scan_failed",
        "error_kind": "gemini_pre_scan_failed",
        "last_error": "503",
        "attempts": 2,
        "model_used": "gemini-2.5-flash",
    })
    return folder


def test_retry_prescan_success_promotes_to_uploaded(monkeypatch, client):
    c, root = client
    folder = _seed_pre_scan_failed(root, "stuck")

    monkeypatch.setattr(api_main, "split_single_file", lambda pdf_path, **kw: SplitOutcome(
        source=str(pdf_path), status="single_factura", n_pages=2, n_facturas=1,
    ))

    r = c.post("/api/books/gastos/files/stuck/retry-prescan")
    assert r.status_code == 200
    body = r.json()
    assert body["doc_id"] == "stuck"
    assert body["pre_scan"]["status"] == "single"
    # El sidecar terminó en uploaded (retrying → uploaded vía nuestro append).
    assert state_writer.current_status(folder) == "uploaded"


def test_retry_prescan_still_failing_keeps_pre_scan_failed(monkeypatch, client):
    c, root = client
    folder = _seed_pre_scan_failed(root, "stuck2")

    monkeypatch.setattr(api_main, "split_single_file", lambda pdf_path, **kw: SplitOutcome(
        source=str(pdf_path), status="error", n_pages=2, error="gemini: still down",
    ))

    r = c.post("/api/books/gastos/files/stuck2/retry-prescan")
    assert r.status_code == 200
    body = r.json()
    assert body["pre_scan"]["status"] == "failed"
    assert state_writer.current_status(folder) == "pre_scan_failed"


def test_retry_prescan_rejects_when_not_in_pre_scan_failed(client):
    c, root = client
    folder = root / "asientos" / "compras_ok"
    folder.mkdir(parents=True)
    state_writer.init_uploaded(folder, "ok", "x", "f" * 64, 1)

    r = c.post("/api/books/gastos/files/ok/retry-prescan")
    assert r.status_code == 400
    assert "no está en pre_scan_failed" in r.json()["detail"]


def test_retry_prescan_404_if_asiento_missing(client):
    c, _ = client
    r = c.post("/api/books/gastos/files/inexistente/retry-prescan")
    assert r.status_code == 404


# ──────────────────────────────────────────────────────────
# /override-prescan endpoint
# ──────────────────────────────────────────────────────────


def test_override_prescan_marks_as_uploaded(client):
    c, root = client
    folder = _seed_pre_scan_failed(root, "force_single")

    r = c.post(
        "/api/books/gastos/files/force_single/override-prescan",
        json={"as_single": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["forced_as_single"] is True
    assert body["status"] == "uploaded"
    assert state_writer.current_status(folder) == "uploaded"


def test_override_prescan_rejects_when_not_in_pre_scan_failed(client):
    c, root = client
    folder = root / "asientos" / "compras_normal"
    folder.mkdir(parents=True)
    state_writer.init_uploaded(folder, "normal", "x", "f" * 64, 1)

    r = c.post(
        "/api/books/gastos/files/normal/override-prescan",
        json={"as_single": True},
    )
    assert r.status_code == 400


def test_override_prescan_rejects_as_single_false(client):
    c, root = client
    _seed_pre_scan_failed(root, "no_single")

    r = c.post(
        "/api/books/gastos/files/no_single/override-prescan",
        json={"as_single": False},
    )
    assert r.status_code == 400
    assert "as_single" in r.json()["detail"]


# ──────────────────────────────────────────────────────────
# Timeout del pre-scan → degradación no bloqueante (deferred)
# ──────────────────────────────────────────────────────────


def _patch_prescan_timeout(monkeypatch):
    """Fuerza que el ``asyncio.wait_for`` que envuelve el ``to_thread`` del
    pre-scan expire de inmediato, sin esperar segundos reales.

    Selectivo: solo dispara TimeoutError sobre la corrutina de
    ``asyncio.to_thread`` (``co_name == "to_thread"``); cualquier otra
    corrutina se delega al ``wait_for`` real para no romper el TestClient.
    El coro se cierra para evitar el warning "coroutine was never awaited" y
    que el thread llegue a arrancar (no deja hilo huérfano en el test).
    """
    import asyncio as _asyncio

    real_wait_for = _asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "to_thread":
            if hasattr(coro, "close"):
                coro.close()
            raise _asyncio.TimeoutError
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(api_main.asyncio, "wait_for", fake_wait_for)


def test_upload_prescan_timeout_is_non_blocking(monkeypatch, client):
    """Timeout del pre-scan: NO bloquea. El archivo queda ``uploaded`` y el
    pre_scan se reporta ``deferred`` (se dividirá al escanear).
    """
    c, root = client

    # split_single_file no debería llegar a ejecutarse (wait_for expira antes),
    # pero lo stubbeamos para no tocar Gemini si la selección fallara.
    monkeypatch.setattr(api_main, "split_single_file", lambda *a, **kw: SplitOutcome(
        source="x", status="single_page", n_pages=1, n_facturas=1,
    ))
    _patch_prescan_timeout(monkeypatch)

    r = _upload(c, "lento.pdf", _pdf_bytes(11))
    assert r.status_code == 200
    body = r.json()

    assert body["detected_summary"]["files_blocked"] == 0
    assert body["detected_summary"]["files_skipped"] == 1
    assert body["detected_summary"]["total_invoices"] == 1

    [file_info] = body["files"]
    pre = file_info["pre_scan"]
    assert pre["status"] == "deferred"
    assert pre["detected_invoices"] == 1
    assert pre["error"] is None

    # El sidecar NO debe quedar bloqueado: sigue en uploaded para que el
    # splitter safeguard de pipeline.run lo divida al escanear.
    asiento = root / "asientos" / "compras_lento"
    assert state_writer.current_status(asiento) == "uploaded"


def test_retry_prescan_timeout_promotes_to_uploaded(monkeypatch, client):
    """Retry de un ``pre_scan_failed`` que vuelve a hacer timeout: desbloquea a
    ``uploaded`` (deferred) en vez de re-bloquear.
    """
    c, root = client
    folder = _seed_pre_scan_failed(root, "lento_retry")

    monkeypatch.setattr(api_main, "split_single_file", lambda *a, **kw: SplitOutcome(
        source="x", status="single_page", n_pages=1, n_facturas=1,
    ))
    _patch_prescan_timeout(monkeypatch)

    r = c.post("/api/books/gastos/files/lento_retry/retry-prescan")
    assert r.status_code == 200
    body = r.json()
    assert body["pre_scan"]["status"] == "deferred"
    assert state_writer.current_status(folder) == "uploaded"


def test_reupload_fully_processed_single_is_informational(monkeypatch, client):
    """Re-subir un PDF de factura única ya completada (done) NO es error
    técnico: aparece en already_processed con total/done, no en errors.
    """
    c, root = client
    content = _pdf_bytes(7)

    monkeypatch.setattr(api_main, "split_single_file", lambda pdf_path, **kw: SplitOutcome(
        source=str(pdf_path), status="single_page", n_pages=1, n_facturas=1,
    ))
    r1 = _upload(c, "hecha.pdf", content)
    assert r1.status_code == 200
    assert r1.json()["uploaded"] == ["hecha.pdf"]

    # Simulamos procesado completo: sidecar → done.
    asiento = root / "asientos" / "compras_hecha"
    state_writer.append(asiento, {"status": "done", "decision": "auto"})

    # Re-subida del mismo sha256.
    r2 = _upload(c, "hecha.pdf", content)
    assert r2.status_code == 200
    body2 = r2.json()

    assert body2["uploaded"] == []
    assert not any("duplicate" in (e.get("error") or "") for e in body2["errors"])
    assert len(body2["already_processed"]) == 1
    ap = body2["already_processed"][0]
    assert ap["original"] == "compras_hecha"
    assert ap["original_status"] == "done"
    assert ap["total"] == 1
    assert ap["done"] == 1
    assert ap["pending"] == []


def test_reupload_fully_closed_split_is_informational(monkeypatch, client):
    """Re-subir un PDF-lote cuyos hijos están todos cerrados → already_processed
    con total=N, done=N y pending=[]; nunca errors.
    """
    c, root = client
    asientos = root / "asientos"
    content = _pdf_bytes(8)

    def fake_split(pdf_path, *, client=None, model=None, max_attempts=None):
        pdf = Path(pdf_path)
        parent = asientos / f"compras_{pdf.stem}"
        children = [f"{pdf.stem}__1of2", f"{pdf.stem}__2of2"]
        state_writer.append(parent, {
            "status": "split",
            "split_into": children,
            "archived_pdf": f"_originales/{pdf.name}",
        })
        for i, ch in enumerate(children, start=1):
            cf = asientos / f"compras_{ch}"
            state_writer.init_uploaded(cf, ch, f"{ch}.pdf", f"sha-{i}" + "0" * 60, 100)
            state_writer.append(cf, {"status": "done", "decision": "auto"})
        return SplitOutcome(
            source=str(pdf), status="split", n_pages=2, n_facturas=2,
            outputs=[f"{ch}.pdf" for ch in children],
        )
    monkeypatch.setattr(api_main, "split_single_file", fake_split)

    r1 = _upload(c, "lote2.pdf", content)
    assert r1.status_code == 200
    assert r1.json()["files"][0]["pre_scan"]["status"] == "split"

    r2 = _upload(c, "lote2.pdf", content)
    assert r2.status_code == 200
    body2 = r2.json()
    assert not any("duplicate" in (e.get("error") or "") for e in body2["errors"])
    assert len(body2["already_processed"]) == 1
    ap = body2["already_processed"][0]
    assert ap["original_status"] == "split"
    assert ap["total"] == 2
    assert ap["done"] == 2
    assert ap["pending"] == []
