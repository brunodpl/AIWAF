"""Tests del ciclo de vida ampliado del sidecar (.state.json).

Cubre:

- Lifecycle completo: uploaded → processing → done → confirmed.
- Lifecycle con retry: uploaded → processing → error → retrying → done.
- Cancel mid-pipeline: uploaded → processing → cancelled.
- Duplicado físico (find_by_file_sha256).
- Duplicado fiscal (find_by_fiscal_hash).
- ``is_confirmed`` legacy: sidecar con ``done+action=confirmed`` devuelve True.
- ``is_pending_confirm`` solo si último status es ``done`` exactamente.
- ``is_terminal`` excluye ``done`` (no es terminal — espera confirm humano).
- Transiciones inválidas: emiten WARNING al log, no lanzan, sidecar se escribe.
- Status válido nuevo: ``init_uploaded`` rechaza sidecar preexistente.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src import state_writer
from src.state_writer import (
    SCHEMA_V,
    SIDECAR_NAME,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    append,
    current_status,
    find_by_file_sha256,
    find_by_fiscal_hash,
    init_uploaded,
    is_confirmed,
    is_pending_confirm,
    is_terminal,
    read,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "asientos" / name
    f.mkdir(parents=True)
    return f


# --- VALID_STATUSES amplio ------------------------------------------------


def test_valid_statuses_includes_new_lifecycle_states() -> None:
    for s in ("uploaded", "processing", "retrying", "review", "done",
              "confirmed", "blocked", "cancelled", "error", "renamed", "reset"):
        assert s in VALID_STATUSES, f"status {s!r} debe estar en VALID_STATUSES"


# --- init_uploaded --------------------------------------------------------


def test_init_uploaded_creates_sidecar_with_uploaded_event(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_factura_x")

    init_uploaded(
        folder,
        doc_id="factura_x",
        file_origin="/libros/facturas/compras/factura_x.pdf",
        sha256_file=SHA_A,
        size_bytes=12345,
    )

    data = json.loads((folder / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert data["schema_v"] == SCHEMA_V
    assert data["doc_id"] == "factura_x"
    assert len(data["events"]) == 1
    ev = data["events"][0]
    assert ev["status"] == "uploaded"
    assert ev["sha256_file"] == SHA_A
    assert ev["size_bytes"] == 12345
    assert ev["folder"] == folder.name


def test_init_uploaded_raises_if_sidecar_exists(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_factura_y")
    init_uploaded(folder, "factura_y", "x.pdf", SHA_A, 100)

    with pytest.raises(FileExistsError):
        init_uploaded(folder, "factura_y", "x.pdf", SHA_A, 100)


# --- Lifecycle completo ---------------------------------------------------


def test_full_lifecycle_uploaded_to_confirmed(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_factura_1")
    init_uploaded(folder, "factura_1", "f.pdf", SHA_A, 1024)

    append(folder, {"status": "processing"})
    append(folder, {"status": "done", "decision": "auto"})
    append(folder, {
        "status": "confirmed",
        "actor": "user:bruno",
        "campos_finales": {"total_euros": "100.00"},
    })

    statuses = [ev["status"] for ev in read(folder)["events"]]
    assert statuses == ["uploaded", "processing", "done", "confirmed"]
    assert is_confirmed(folder) is True
    assert is_terminal(folder) is True
    assert is_pending_confirm(folder) is False


def test_lifecycle_with_retry(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_factura_2")
    init_uploaded(folder, "factura_2", "f.pdf", SHA_A, 1024)
    append(folder, {"status": "processing"})
    append(folder, {"status": "error", "motivo": "Timeout Cloud Vision"})
    append(folder, {"status": "retrying", "previous_status": "error", "attempt": 2})
    append(folder, {"status": "processing"})
    append(folder, {"status": "done", "decision": "auto"})

    statuses = [ev["status"] for ev in read(folder)["events"]]
    assert statuses == ["uploaded", "processing", "error", "retrying", "processing", "done"]
    assert is_pending_confirm(folder) is True
    assert is_terminal(folder) is False  # done no es terminal


def test_cancelled_mid_pipeline(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_factura_3")
    init_uploaded(folder, "factura_3", "f.pdf", SHA_A, 1024)
    append(folder, {"status": "processing"})
    append(folder, {"status": "cancelled", "actor": "system", "reason": "user_cancel"})

    assert current_status(folder) == "cancelled"
    assert is_terminal(folder) is True


# --- is_confirmed back-compat (sidecar legacy) ----------------------------


def test_is_confirmed_legacy_done_action_confirmed(tmp_path: Path) -> None:
    """Sidecars escritos antes del refactor usan done+action=confirmed."""
    folder = _folder(tmp_path, "compras_factura_legacy")
    init_uploaded(folder, "factura_legacy", "f.pdf", SHA_A, 1024)
    append(folder, {"status": "processing"})
    append(folder, {"status": "done", "decision": "auto"})
    # Evento "legacy confirm": status="done" + action="confirmed".
    append(folder, {"status": "done", "action": "confirmed", "actor": "user:bruno"})

    assert current_status(folder) == "done"  # no es "confirmed" en el último
    assert is_confirmed(folder) is True       # pero is_confirmed lo detecta


def test_is_pending_confirm_only_when_last_is_done(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_pending")
    init_uploaded(folder, "p", "p.pdf", SHA_A, 1)
    append(folder, {"status": "processing"})
    append(folder, {"status": "review", "decision": "warn"})
    assert is_pending_confirm(folder) is False

    append(folder, {"status": "done"})
    assert is_pending_confirm(folder) is True

    append(folder, {"status": "confirmed", "actor": "user:bruno"})
    assert is_pending_confirm(folder) is False


# --- is_terminal ----------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    ("uploaded", False),
    ("processing", False),
    ("review", False),
    ("done", False),       # done NO es terminal
    ("confirmed", True),
    ("blocked", True),
    ("cancelled", True),
    ("error", True),
    ("pre_scan_failed", True),  # terminal para pipeline.run, requiere acción humana
])
def test_is_terminal_by_last_status(tmp_path: Path, status: str, expected: bool) -> None:
    folder = _folder(tmp_path, f"compras_term_{status}")
    init_uploaded(folder, status, "f.pdf", SHA_A, 1)
    if status != "uploaded":
        append(folder, {"status": status, "decision": "auto", "motivo": "test"} if status == "blocked" else {"status": status})
    assert is_terminal(folder) is expected


# --- find_by_file_sha256 --------------------------------------------------


def test_find_by_file_sha256_locates_existing(tmp_path: Path) -> None:
    asientos = tmp_path / "asientos"
    asientos.mkdir()

    f_a = asientos / "compras_a"
    f_a.mkdir()
    init_uploaded(f_a, "a", "a.pdf", SHA_A, 100)

    f_b = asientos / "compras_b"
    f_b.mkdir()
    init_uploaded(f_b, "b", "b.pdf", SHA_B, 200)

    assert find_by_file_sha256(asientos, SHA_A) == f_a
    assert find_by_file_sha256(asientos, SHA_B) == f_b
    assert find_by_file_sha256(asientos, "c" * 64) is None


# --- find_by_fiscal_hash --------------------------------------------------


def test_find_by_fiscal_hash_locates_in_any_event(tmp_path: Path) -> None:
    """El fiscal_hash se anota en el evento de cierre (review/done/blocked)."""
    asientos = tmp_path / "asientos"
    asientos.mkdir()

    f_a = asientos / "compras_a"
    f_a.mkdir()
    init_uploaded(f_a, "a", "a.pdf", SHA_A, 100)
    append(f_a, {"status": "processing"})
    append(f_a, {"status": "done", "decision": "auto", "fiscal_hash": "[REDACTED]"})

    f_b = asientos / "compras_b"
    f_b.mkdir()
    init_uploaded(f_b, "b", "b.pdf", SHA_B, 200)
    append(f_b, {"status": "processing"})
    append(f_b, {"status": "review", "decision": "warn", "fiscal_hash": "zzz999"})

    assert find_by_fiscal_hash(asientos, "[REDACTED]") == f_a
    assert find_by_fiscal_hash(asientos, "zzz999") == f_b
    assert find_by_fiscal_hash(asientos, "nope") is None


# --- Transiciones inválidas (warning, no lanza) ---------------------------


def test_invalid_transition_emits_warning_but_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = _folder(tmp_path, "compras_bad_trans")
    init_uploaded(folder, "bt", "f.pdf", SHA_A, 1)
    # uploaded -> confirmed no está en VALID_TRANSITIONS["uploaded"]
    assert "confirmed" not in VALID_TRANSITIONS["uploaded"]

    with caplog.at_level(logging.WARNING, logger="pipeline.state"):
        append(folder, {"status": "confirmed", "actor": "test"})

    # Sidecar SÍ se escribe (resiliencia).
    statuses = [ev["status"] for ev in read(folder)["events"]]
    assert statuses == ["uploaded", "confirmed"]
    # Y queda registro en log técnico.
    assert any("transición inusual" in rec.message for rec in caplog.records)


def test_invalid_status_value_still_raises(tmp_path: Path) -> None:
    folder = _folder(tmp_path, "compras_bad_status")
    init_uploaded(folder, "bs", "f.pdf", SHA_A, 1)
    with pytest.raises(ValueError, match="status inválido"):
        append(folder, {"status": "nope"})


# --- _was_rejected_by_human (pipeline.py) ---------------------------------


def test_was_rejected_by_human_detects_reject_event(tmp_path: Path) -> None:
    """Regression: dup fiscal NO debe bloquear si el original fue rechazado."""
    from src.pipeline import _was_rejected_by_human

    folder = _folder(tmp_path, "compras_rejected")
    init_uploaded(folder, "rj", "f.pdf", SHA_A, 1)
    append(folder, {"status": "processing"})
    append(folder, {"status": "review", "decision": "warn"})
    # El operario rechaza via POST /api/invoices/.../action — mapea a
    # status=review + action=reject.
    append(folder, {"status": "review", "actor": "user", "action": "reject"})

    assert _was_rejected_by_human(folder) is True


def test_was_rejected_by_human_false_when_no_reject(tmp_path: Path) -> None:
    from src.pipeline import _was_rejected_by_human

    folder = _folder(tmp_path, "compras_normal")
    init_uploaded(folder, "n", "f.pdf", SHA_A, 1)
    append(folder, {"status": "processing"})
    append(folder, {"status": "done", "decision": "auto"})
    append(folder, {"status": "review", "actor": "user", "action": "approve"})

    assert _was_rejected_by_human(folder) is False


def test_was_rejected_by_human_false_when_folder_missing(tmp_path: Path) -> None:
    from src.pipeline import _was_rejected_by_human

    nonexistent = tmp_path / "no_existe"
    assert _was_rejected_by_human(nonexistent) is False


# --- rejection_count ------------------------------------------------------


def test_rejection_count_counts_user_reject_events(tmp_path):
    folder = tmp_path / "compras_doc_abc"
    state_writer.init_uploaded(folder, "doc_abc", "in/abc.pdf", "sha", 100)
    state_writer.append(folder, {"status": "processing"})
    # rechazo automático del orquestador — no cuenta
    state_writer.append(folder, {"status": "review", "actor": "system"})
    assert state_writer.rejection_count(folder) == 0
    # rechazo manual del operario — cuenta
    state_writer.append(folder, {"status": "review", "actor": "user", "action": "reject"})
    assert state_writer.rejection_count(folder) == 1
    # segundo rechazo del operario tras nuevo intento
    state_writer.append(folder, {"status": "processing"})
    state_writer.append(folder, {"status": "review", "actor": "user", "action": "reject"})
    assert state_writer.rejection_count(folder) == 2


def test_rejection_count_zero_when_no_sidecar(tmp_path):
    folder = tmp_path / "nonexistent"
    assert state_writer.rejection_count(folder) == 0
