"""Tests del carry-over de facturas en status=review al siguiente lote.

Task 3 del refactor: cuando el pipeline arranca un nuevo run y escribe
``.pending_confirm.json``, debe incluir las carpetas de asiento cuyo
sidecar quedó en ``review`` (rechazo del operario en un run previo).
Así el reviewer las muestra una vez más; si el operario las rechaza por
segunda vez, el frontend dispara hard-delete.

Cobertura:
  - Run con 1 nuevo + 1 review previo → ambos en pending_confirm.
  - Run sin carry-over (asientos sólo en done/confirmed) → solo nuevos.
  - Re-subida con mismo doc_id que un review → dedup, aparece una vez.
  - Sidecar corrupto en una carpeta vecina → no rompe el resto del lote.
  - Carry-over es determinista (orden por nombre de carpeta).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import state_writer
from src.pipeline import _append_review_carryover


@pytest.fixture
def asientos_root(tmp_path: Path) -> Path:
    root = tmp_path / "asientos"
    root.mkdir(parents=True)
    return root


def _make_folder(
    asientos_root: Path,
    folder_name: str,
    doc_id: str,
    last_status: str,
) -> Path:
    """Crea una carpeta de asiento con sidecar y termina en ``last_status``."""
    folder = asientos_root / folder_name
    state_writer.init_uploaded(
        folder,
        doc_id=doc_id,
        file_origin=f"facturas/compras/{doc_id}.pdf",
        sha256_file="a" * 64,
        size_bytes=100,
    )
    if last_status != "uploaded":
        state_writer.append(folder, {"status": last_status})
    return folder


def test_carryover_includes_review_folder(asientos_root: Path):
    """Un review previo se añade al lote nuevo."""
    _make_folder(asientos_root, "compras_rechazado", "rechazado", "review")

    out = _append_review_carryover(asientos_root, ["nuevo_001"])

    assert out == ["nuevo_001", "rechazado"]


def test_carryover_empty_when_no_review_folders(asientos_root: Path):
    """Asientos en done/confirmed/blocked no se arrastran — solo review."""
    _make_folder(asientos_root, "compras_done", "doc_done", "done")
    _make_folder(asientos_root, "compras_confirmed", "doc_confirmed", "confirmed")
    _make_folder(asientos_root, "compras_blocked", "doc_blocked", "blocked")
    _make_folder(asientos_root, "compras_error", "doc_error", "error")
    _make_folder(asientos_root, "compras_uploaded", "doc_uploaded", "uploaded")

    out = _append_review_carryover(asientos_root, ["nuevo_001"])

    assert out == ["nuevo_001"]


def test_carryover_dedupes_when_doc_id_already_in_batch(asientos_root: Path):
    """Re-subida: el doc_id nuevo ya coincide con un review previo → sólo una vez."""
    _make_folder(asientos_root, "compras_resubido", "doc_resubido", "review")

    out = _append_review_carryover(asientos_root, ["doc_resubido", "nuevo_002"])

    assert out == ["doc_resubido", "nuevo_002"]
    assert out.count("doc_resubido") == 1


def test_carryover_returns_new_list_does_not_mutate_input(asientos_root: Path):
    """La función no muta su argumento."""
    _make_folder(asientos_root, "compras_x", "doc_x", "review")
    original = ["nuevo_a"]

    out = _append_review_carryover(asientos_root, original)

    assert original == ["nuevo_a"]  # Sin mutación
    assert out == ["nuevo_a", "doc_x"]


def test_carryover_handles_missing_asientos_root(tmp_path: Path):
    """Si la raíz aún no existe (primer run del sistema), devuelve la lista tal cual."""
    out = _append_review_carryover(tmp_path / "noexiste", ["nuevo"])
    assert out == ["nuevo"]


def test_carryover_skips_corrupt_sidecar(asientos_root: Path, caplog):
    """Un sidecar ilegible no rompe el carry-over de los demás."""
    _make_folder(asientos_root, "compras_good", "doc_good", "review")

    # Carpeta con sidecar corrupto.
    corrupt_dir = asientos_root / "compras_corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / ".state.json").write_text("{ not valid json", encoding="utf-8")

    out = _append_review_carryover(asientos_root, ["nuevo"])

    assert "doc_good" in out
    assert out == ["nuevo", "doc_good"]


def test_carryover_skips_folder_without_sidecar(asientos_root: Path):
    """Carpetas sin sidecar (basura del filesystem) se ignoran sin warning."""
    (asientos_root / "sin_sidecar").mkdir()
    _make_folder(asientos_root, "compras_review", "doc_review", "review")

    out = _append_review_carryover(asientos_root, [])

    assert out == ["doc_review"]


def test_carryover_skips_files_in_asientos_root(asientos_root: Path):
    """Si alguien deja un fichero suelto en asientos/, no lo confundimos con carpeta."""
    (asientos_root / "ruido.txt").write_text("x", encoding="utf-8")
    _make_folder(asientos_root, "compras_review", "doc_review", "review")

    out = _append_review_carryover(asientos_root, [])

    assert out == ["doc_review"]


def test_carryover_is_deterministic_order(asientos_root: Path):
    """Múltiples reviews se añaden ordenados por nombre de carpeta — orden estable."""
    _make_folder(asientos_root, "compras_zzz", "doc_zzz", "review")
    _make_folder(asientos_root, "compras_aaa", "doc_aaa", "review")
    _make_folder(asientos_root, "compras_mmm", "doc_mmm", "review")

    out = _append_review_carryover(asientos_root, ["nuevo"])

    # Nuevos primero; carry-overs detrás en orden alfabético del folder name.
    assert out == ["nuevo", "doc_aaa", "doc_mmm", "doc_zzz"]


def test_carryover_pending_confirm_json_includes_review(tmp_path: Path):
    """Smoke test del flujo completo: _write_pending_confirm con carry-over.

    Comprueba que tras el hook el JSON serializado contiene ambos doc_ids.
    """
    from types import SimpleNamespace
    from src.pipeline import _write_pending_confirm

    asientos = tmp_path / "asientos"
    runtime = tmp_path / ".runtime"
    asientos.mkdir()
    runtime.mkdir()

    _make_folder(asientos, "compras_rechazado", "rechazado_doc", "review")

    cfg = SimpleNamespace(
        asientos_path=lambda: str(asientos),
        runtime_path=lambda: str(runtime),
    )

    merged = _append_review_carryover(cfg.asientos_path(), ["nuevo_doc"])
    _write_pending_confirm(cfg, merged)

    data = json.loads((runtime / ".pending_confirm.json").read_text(encoding="utf-8"))
    assert set(data["doc_ids_lote"]) == {"nuevo_doc", "rechazado_doc"}
    assert data["doc_ids_lote"][0] == "nuevo_doc"  # Nuevos primero
