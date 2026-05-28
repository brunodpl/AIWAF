"""Tests del estado `split` (sidecar del PDF original tras Fase 1 splitter)."""

from __future__ import annotations

from pathlib import Path


from src.state_writer import (
    VALID_STATUSES,
    VALID_TRANSITIONS,
    append,
    current_status,
    init_uploaded,
    is_split,
    is_terminal,
    read,
)


SHA = "f" * 64


def _folder(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "asientos" / name
    f.mkdir(parents=True)
    return f


def test_split_is_valid_status_and_terminal_from_uploaded(tmp_path):
    assert "split" in VALID_STATUSES
    # uploaded -> split debe estar permitido sin warning
    assert "split" in VALID_TRANSITIONS["uploaded"]
    assert "split" in VALID_TRANSITIONS["processing"]

    folder = _folder(tmp_path, "compras_1_2_3_merged")
    init_uploaded(folder, "1_2_3_merged", "facturas/compras/1_2_3_merged.pdf", SHA, 12345)
    append(folder, {
        "status": "split",
        "split_into": ["1_2_3_merged__1of3", "1_2_3_merged__2of3", "1_2_3_merged__3of3"],
        "archived_pdf": "facturas/compras/_originales/1_2_3_merged.pdf",
    })

    assert current_status(folder) == "split"
    assert is_split(folder) is True
    assert is_terminal(folder) is True


def test_split_event_preserves_split_into_and_archived_pdf(tmp_path):
    folder = _folder(tmp_path, "compras_doc")
    init_uploaded(folder, "doc", "facturas/compras/doc.pdf", SHA, 100)
    append(folder, {
        "status": "split",
        "split_into": ["doc__1of2", "doc__2of2"],
        "archived_pdf": "facturas/compras/_originales/doc.pdf",
    })

    data = read(folder)
    last = data["events"][-1]
    assert last["status"] == "split"
    assert last["split_into"] == ["doc__1of2", "doc__2of2"]
    assert last["archived_pdf"].endswith("doc.pdf")


def test_split_is_terminal_blocks_pipeline_skip_loop(tmp_path):
    """is_terminal=True asegura que pipeline.run_pipeline skipea esta carpeta."""
    folder = _folder(tmp_path, "compras_other")
    init_uploaded(folder, "other", "facturas/compras/other.pdf", SHA, 100)
    append(folder, {"status": "split", "split_into": ["other__1of2"]})
    assert is_terminal(folder) is True
