"""Tests para el sidecar de estado por documento (.state.json).

Verifica:
- Inicialización correcta del sidecar
- Append-only: ningún evento previo se sobreescribe
- Atomicidad: tmp + os.replace (no quedan ficheros parciales en errores)
- Renombrado con resolución de colisiones (sufijo _2, _3...)
- Lookup por doc_id escaneando la raíz de asientos
"""

import json
import os
from pathlib import Path

import pytest

import src.state_writer as sw
from src import state_writer
from src.state_writer import (
    SCHEMA_V,
    SIDECAR_NAME,
    append,
    current_status,
    find_folder_by_doc_id,
    init,
    read,
    rename,
)


def _make_folder(tmp_path: Path, name: str = "compras_factura_test") -> Path:
    folder = tmp_path / "asientos" / name
    folder.mkdir(parents=True)
    return folder


# --- init ----------------------------------------------------------------


def test_init_creates_sidecar_with_processing_event(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)

    init(folder, doc_id="factura_test", file_origin="libros/facturas/compras/factura_test.pdf")

    sidecar = folder / SIDECAR_NAME
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_v"] == SCHEMA_V
    assert data["doc_id"] == "factura_test"
    assert len(data["events"]) == 1
    ev = data["events"][0]
    assert ev["status"] == "processing"
    assert ev["file_origin"] == "libros/facturas/compras/factura_test.pdf"
    assert ev["folder"] == folder.name
    assert "ts" in ev


def test_init_idempotent_on_existing_sidecar(tmp_path: Path) -> None:
    """Reproceso: init sobre una carpeta ya iniciada añade evento processing nuevo."""
    folder = _make_folder(tmp_path)
    init(folder, doc_id="factura_test", file_origin="origen1.pdf")
    init(folder, doc_id="factura_test", file_origin="origen2.pdf")

    data = read(folder)
    assert data["doc_id"] == "factura_test"  # cabecera intacta
    assert len(data["events"]) == 2
    assert all(e["status"] == "processing" for e in data["events"])


# --- append --------------------------------------------------------------


def test_append_adds_event_at_end(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    init(folder, doc_id="factura_test", file_origin="x.pdf")

    append(folder, {"status": "review", "decision": "warn", "motivos": ["NIF no resuelto"]})

    data = read(folder)
    assert len(data["events"]) == 2
    last = data["events"][-1]
    assert last["status"] == "review"
    assert last["decision"] == "warn"
    assert last["motivos"] == ["NIF no resuelto"]
    assert "ts" in last


def test_append_never_mutates_previous_events(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    init(folder, doc_id="factura_test", file_origin="x.pdf")
    first_event = read(folder)["events"][0].copy()

    append(folder, {"status": "done", "decision": "auto"})
    append(folder, {"status": "review", "decision": "warn"})

    events = read(folder)["events"]
    assert events[0] == first_event  # primer evento intocable
    assert len(events) == 3


def test_append_raises_if_no_sidecar(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    with pytest.raises(FileNotFoundError):
        append(folder, {"status": "done"})


def test_append_rejects_invalid_status(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    init(folder, doc_id="x", file_origin="x.pdf")
    with pytest.raises(ValueError):
        append(folder, {"status": "weird_status"})


# --- current_status ------------------------------------------------------


def test_current_status_returns_last_event_status(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    assert current_status(folder) is None
    init(folder, doc_id="x", file_origin="x.pdf")
    assert current_status(folder) == "processing"
    append(folder, {"status": "done", "decision": "auto"})
    assert current_status(folder) == "done"


# --- rename --------------------------------------------------------------


def test_rename_moves_folder_and_emits_event(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path, "compras_factura_xyz")
    init(folder, doc_id="factura_xyz", file_origin="x.pdf")

    new_path = rename(folder, "compras_2026-04-15_A12345678_F-2025-001")

    assert not folder.exists()
    assert new_path.exists()
    assert new_path.name == "compras_2026-04-15_A12345678_F-2025-001"
    data = read(new_path)
    last = data["events"][-1]
    assert last["status"] == "renamed"
    assert last["from"] == "compras_factura_xyz"
    assert last["to"] == "compras_2026-04-15_A12345678_F-2025-001"
    assert "collision" not in last


def test_rename_resolves_collision_with_suffix(tmp_path: Path) -> None:
    asientos = tmp_path / "asientos"
    asientos.mkdir()

    # Carpeta destino ya existe con otro contenido
    target_taken = asientos / "compras_2026-04-15_A12345678_F-2025-001"
    target_taken.mkdir()
    (target_taken / "marker.txt").write_text("ocupada")

    folder = asientos / "compras_factura_otra"
    folder.mkdir()
    init(folder, doc_id="factura_otra", file_origin="x.pdf")

    new_path = rename(folder, "compras_2026-04-15_A12345678_F-2025-001")

    assert new_path.name == "compras_2026-04-15_A12345678_F-2025-001_2"
    assert (target_taken / "marker.txt").exists()  # destino original intacto
    last = read(new_path)["events"][-1]
    assert last["status"] == "renamed"
    assert last["collision"] is True
    assert last["to"].endswith("_2")


def test_rename_noop_when_already_named(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path, "compras_already_named")
    init(folder, doc_id="x", file_origin="x.pdf")
    initial_events = len(read(folder)["events"])

    result = rename(folder, "compras_already_named")

    assert result == folder
    assert len(read(folder)["events"]) == initial_events  # no nuevo evento


# --- find_folder_by_doc_id ----------------------------------------------


def test_find_folder_by_doc_id_locates_correct_folder(tmp_path: Path) -> None:
    asientos = tmp_path / "asientos"
    asientos.mkdir()
    f1 = asientos / "compras_a"
    f2 = asientos / "compras_b"
    f1.mkdir()
    f2.mkdir()
    init(f1, doc_id="doc-1", file_origin="a.pdf")
    init(f2, doc_id="doc-2", file_origin="b.pdf")

    assert find_folder_by_doc_id(asientos, "doc-2") == f2
    assert find_folder_by_doc_id(asientos, "doc-1") == f1
    assert find_folder_by_doc_id(asientos, "doc-zzz") is None


def test_find_folder_skips_dirs_without_sidecar(tmp_path: Path) -> None:
    asientos = tmp_path / "asientos"
    asientos.mkdir()
    (asientos / "huerfana").mkdir()
    f = asientos / "buena"
    f.mkdir()
    init(f, doc_id="doc-1", file_origin="x.pdf")

    assert find_folder_by_doc_id(asientos, "doc-1") == f


# --- atomicidad ----------------------------------------------------------


def test_no_tmp_files_left_after_writes(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path)
    init(folder, doc_id="x", file_origin="x.pdf")
    append(folder, {"status": "review", "decision": "warn"})
    append(folder, {"status": "done", "decision": "auto"})

    leftovers = [p for p in folder.iterdir() if p.name.startswith(".state-") and p.suffix == ".tmp"]
    assert leftovers == []


# --- concurrencia (lock por carpeta) -------------------------------------


def test_concurrent_appends_preserve_all_events(tmp_path: Path) -> None:
    """Regresión: 16 appends concurrentes desde threads distintos NO deben
    perder eventos. Sin lock perderíamos algunos por la race
    leer→leer→escribir A→escribir B. Con el lock por carpeta todos persisten.
    """
    import threading

    folder = _make_folder(tmp_path)
    init(folder, doc_id="x", file_origin="x.pdf")

    N_THREADS = 16
    barrier = threading.Barrier(N_THREADS)

    def worker(i: int) -> None:
        barrier.wait()
        append(folder, {"status": "review", "decision": "warn", "i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = read(folder)["events"]
    # 1 inicial (processing) + N_THREADS reviews
    assert len(events) == 1 + N_THREADS
    # Cada `i` debe aparecer exactamente una vez (cero pérdidas).
    indices = sorted(e["i"] for e in events if e.get("status") == "review")
    assert indices == list(range(N_THREADS))


# --- reintentos ante PermissionError transitorio (bind-mount Windows) ----


def test_rename_reintenta_tras_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica que _replace_with_retry reintenta ante PermissionError transitorio
    y que rename() termina con éxito cuando el error desaparece en el 3.er intento."""
    folder = _make_folder(tmp_path, "src_folder")
    init(folder, doc_id="x", file_origin="x.pdf")

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(a, b):
        # Solo interceptar el rename de directorio (no la escritura atómica del sidecar)
        if Path(a).is_dir():
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "locked")
        return real_replace(a, b)

    monkeypatch.setattr(sw.os, "replace", flaky)
    monkeypatch.setattr(sw.time, "sleep", lambda *_: None)  # no real delay

    out = sw.rename(folder, "dst_folder")

    assert out.name == "dst_folder"
    assert calls["n"] == 3
