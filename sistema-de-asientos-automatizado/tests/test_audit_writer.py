"""
Tests para el escritor de auditoría de negocio (audit_writer.py).

Verifica:
- Formato JSONL correcto
- Schema de registro completo
- Resiliencia ante errores de escritura
- Comportamiento append-only
"""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from src.audit_writer import AuditWriter, SCHEMA_VERSION


@dataclass
class FakePhaseResult:
    fase: str
    ok: bool
    motivo: str = ""


def _make_writer(tmp_dir: str, libro: str = "20_COMPRAS_GASTOS") -> AuditWriter:
    return AuditWriter(logs_path=tmp_dir, libro=libro)


def _make_results() -> list:
    return [
        FakePhaseResult("ocr", ok=True),
        FakePhaseResult("identidad_cabecera", ok=True),
        FakePhaseResult("fiscal", ok=True),
        FakePhaseResult("semantica", ok=False, motivo="error técnico en fase semantica"),
        FakePhaseResult("cliente_destino", ok=False, motivo="error o identidad no disponible"),
        FakePhaseResult("ensamblador", ok=True),
    ]


class TestAuditWriterBasic:
    """Verifica escritura básica de registros JSONL."""

    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="auto",
                output_base_path=tmp,
            )
            assert writer._file_path.exists()

    def test_write_produces_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="auto",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert isinstance(record, dict)

    def test_schema_version_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="warn",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())
            assert record["schema_v"] == SCHEMA_VERSION

    def test_required_fields_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="auto",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())

            required_keys = {
                "schema_v", "ts_proceso", "doc_id", "libro",
                "archivo_origen", "fases", "campos_criticos",
                "decision_global", "autocargable", "motivos_revision",
                "verificaciones", "artefactos",
            }
            assert required_keys.issubset(record.keys())

    def test_decision_global_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="block",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())
            assert record["decision_global"] == "block"
            assert record["autocargable"] is False

    def test_autocargable_true_when_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="auto",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())
            assert record["autocargable"] is True


class TestAuditWriterAppend:
    """Verifica que el archivo es append-only."""

    def test_multiple_writes_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            for i in range(3):
                writer.write(
                    doc_id=f"factura_{i:03d}",
                    file_path=f"/input/factura_{i:03d}.pdf",
                    results=_make_results(),
                    decision="auto",
                    output_base_path=tmp,
                )
            with open(writer._file_path, encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 3
            for line in lines:
                record = json.loads(line)
                assert "doc_id" in record


class TestAuditWriterResilience:
    """Verifica que errores de escritura no interrumpen el pipeline."""

    def test_write_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            # Forzar un error de escritura usando un path inválido
            writer._file_path = Path("/nonexistent/path/audit.jsonl")
            # No debe lanzar excepción
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=_make_results(),
                decision="auto",
                output_base_path=tmp,
            )

    def test_none_doc_id_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            writer.write(
                doc_id=None,
                file_path="/input/corrupted.pdf",
                results=[FakePhaseResult("ocr", ok=False, motivo="OCR falló")],
                decision="error",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())
            assert record["doc_id"] == "UNKNOWN"


class TestAuditWriterFases:
    """Verifica que las fases se registran correctamente."""

    def test_fases_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = _make_writer(tmp)
            results = _make_results()
            writer.write(
                doc_id="factura_001",
                file_path="/input/factura_001.pdf",
                results=results,
                decision="auto",
                output_base_path=tmp,
            )
            with open(writer._file_path, encoding="utf-8") as f:
                record = json.loads(f.readline())
            fases = record["fases"]
            assert fases["ocr"]["ok"] is True
            assert fases["semantica"]["ok"] is False
            assert fases["semantica"]["motivo"] is not None
