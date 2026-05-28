"""Tests de la orquestación paralela de run_pipeline (map-paralelo/reduce-serial).

Aíslan el loop + reducer mockeando src.pipeline.process_document — no llaman a
Vision/Gemini ni ejecutan Fase 3 real.
"""

from src.pipeline import PhaseResult


def test_phase_result_has_duration_ms_default():
    """PhaseResult expone duration_ms con default 0.0 (instrumentación)."""
    r = PhaseResult("ocr", ok=True)
    assert r.duration_ms == 0.0
    r2 = PhaseResult("ocr", ok=True, duration_ms=12.5)
    assert r2.duration_ms == 12.5


import json
from pathlib import Path
from src import state_writer
from src.audit_writer import AuditWriter


def _seed_validacion(asientos_root: Path, libro_short: str, doc_id: str,
                     decision: str, **campos) -> Path:
    """Crea una carpeta de asiento con .state.json y resultado_validacion.json."""
    folder = asientos_root / f"{libro_short}_{doc_id}"
    folder.mkdir(parents=True)
    state_writer.init(folder, doc_id=doc_id, file_origin=f"{doc_id}.pdf")
    campos_obj = {k: {"valor_final": v} for k, v in campos.items()}
    (folder / "resultado_validacion.json").write_text(
        json.dumps({"decision_global": decision, "campos": campos_obj}),
        encoding="utf-8",
    )
    return folder


def test_finalize_counts_auto(tmp_path):
    """_finalize_document incrementa summary['ok'] para decision auto."""
    from src.pipeline import _finalize_document, PhaseResult

    asientos = tmp_path / "asientos"
    asientos.mkdir()
    folder = _seed_validacion(asientos, "compras", "f1", "auto",
                              nif_entidad="B1", numero_factura="N1",
                              fecha_expedicion="2026-01-01", nif_cliente="A9")
    audit = AuditWriter(str(tmp_path / "audit"), "20_COMPRAS_GASTOS")
    summary = {"total": 1, "ok": 0, "warn": 0, "error": 0}

    _finalize_document(
        "f1", str(folder), [PhaseResult("ocr", ok=True)], "f1.pdf",
        audit=audit, asientos_root=str(asientos), libro_short="compras",
        summary=summary, status_file=None, processed_offset=0,
    )

    assert summary["ok"] == 1


def test_finalize_blocks_fiscal_duplicate(tmp_path):
    """Dos docs con el mismo triplete fiscal → el segundo finalizado se bloquea."""
    from src.pipeline import _finalize_document, PhaseResult

    asientos = tmp_path / "asientos"
    asientos.mkdir()
    audit = AuditWriter(str(tmp_path / "audit"), "20_COMPRAS_GASTOS")
    summary = {"total": 2, "ok": 0, "warn": 0, "error": 0}

    f1 = _seed_validacion(asientos, "compras", "f1", "auto",
                          nif_entidad="B1", numero_factura="N1",
                          fecha_expedicion="2026-01-01", nif_cliente="A9")
    f2 = _seed_validacion(asientos, "compras", "f2", "auto",
                          nif_entidad="B1", numero_factura="N1",
                          fecha_expedicion="2026-01-01", nif_cliente="A9")

    _finalize_document("f1", str(f1), [PhaseResult("ocr", ok=True)], "f1.pdf",
                       audit=audit, asientos_root=str(asientos),
                       libro_short="compras", summary=summary,
                       status_file=None, processed_offset=0)
    _finalize_document("f2", str(f2), [PhaseResult("ocr", ok=True)], "f2.pdf",
                       audit=audit, asientos_root=str(asientos),
                       libro_short="compras", summary=summary,
                       status_file=None, processed_offset=0)

    assert summary["ok"] == 1
    assert summary["error"] == 1
    assert state_writer.current_status(f2) == "blocked"


import time
from unittest.mock import MagicMock, patch


def _run_with_fake_process(tmp_path, n_files, *, max_concurrency,
                           decision="auto", same_fiscal=False,
                           cancel_after=None):
    """Ejecuta run_pipeline con process_document falso. Devuelve (summary, call_count)."""
    folder = tmp_path / "inbox"
    folder.mkdir()
    for i in range(n_files):
        (folder / f"f{i}.pdf").write_bytes(b"%PDF-1.4 dummy")

    asientos = tmp_path / "asientos"
    asientos.mkdir()

    calls = {"n": 0}

    def fake_process(file_path, folder_name, vision_client, gemini_model,
                     libro, output_base_path):
        calls["n"] += 1
        time.sleep(0.02)  # simula trabajo para forzar solape
        doc_id = Path(file_path).stem
        out = Path(output_base_path) / f"compras_{doc_id}"
        out.mkdir(parents=True, exist_ok=True)
        state_writer.init(out, doc_id=doc_id, file_origin=f"{doc_id}.pdf")
        if same_fiscal:
            campos = {"nif_entidad": {"valor_final": "B1"},
                      "numero_factura": {"valor_final": "N1"},
                      "fecha_expedicion": {"valor_final": "2026-01-01"},
                      "nif_cliente": {"valor_final": "A9"}}
        else:
            campos = {"nif_entidad": {"valor_final": f"B{doc_id}"},
                      "numero_factura": {"valor_final": f"N{doc_id}"},
                      "fecha_expedicion": {"valor_final": "2026-01-01"},
                      "nif_cliente": {"valor_final": "A9"}}
        (out / "resultado_validacion.json").write_text(
            json.dumps({"decision_global": decision, "campos": campos}), encoding="utf-8")
        return doc_id, str(out), [PhaseResult("ocr", ok=True, duration_ms=1.0)]

    cancel_state = {"done": 0}
    def cancel_requested():
        if cancel_after is None:
            return False
        return cancel_state["done"] >= cancel_after

    cfg = MagicMock()
    cfg.pipeline_max_concurrency = max_concurrency
    cfg.asientos_path.return_value = str(asientos)
    cfg.audit_path.return_value = str(tmp_path / "audit")
    cfg.runtime_path.return_value = str(tmp_path / "runtime")
    cfg.logs_path = str(tmp_path / "logs")
    cfg.extensiones_list = ["pdf"]

    import src.pipeline as plmod
    orig_finalize = plmod._finalize_document
    def counting_finalize(*a, **kw):
        orig_finalize(*a, **kw)
        cancel_state["done"] += 1

    with patch("src.pipeline.get_settings", return_value=cfg), \
         patch("src.pipeline.VisionOcrClient", return_value=MagicMock()), \
         patch("src.pipeline._init_gemini_model", return_value=MagicMock()), \
         patch("src.pipeline.run_split", return_value=[]), \
         patch("src.pipeline.process_document", side_effect=fake_process), \
         patch("src.pipeline._finalize_document", side_effect=counting_finalize):
        from src.pipeline import run_pipeline
        summary = run_pipeline(str(folder), "20_COMPRAS_GASTOS",
                               cancel_requested=cancel_requested)
    return summary, calls["n"]


def test_parallel_processes_all_docs(tmp_path):
    summary, n = _run_with_fake_process(tmp_path, 5, max_concurrency=3)
    assert summary["total"] == 5
    assert summary["ok"] == 5
    assert n == 5


def test_serial_fallback_max_concurrency_1(tmp_path):
    summary, n = _run_with_fake_process(tmp_path, 3, max_concurrency=1)
    assert summary["total"] == 3
    assert summary["ok"] == 3


def test_parallel_dedup_blocks_one(tmp_path):
    summary, _ = _run_with_fake_process(tmp_path, 2, max_concurrency=2,
                                        same_fiscal=True)
    assert summary["ok"] == 1
    assert summary["error"] == 1  # block → error


def test_summary_has_elapsed_s(tmp_path):
    summary, _ = _run_with_fake_process(tmp_path, 2, max_concurrency=2)
    assert "elapsed_s" in summary
    assert summary["elapsed_s"] >= 0


def test_cancellation_stops_submitting(tmp_path):
    summary, n = _run_with_fake_process(tmp_path, 12, max_concurrency=2,
                                        cancel_after=1)
    assert n < 12
