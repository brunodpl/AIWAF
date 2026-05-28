"""
Tests del ensamblador (fase 4).

Verifica lógica de ensamblado, decisión global y verificaciones cruzadas.
No requiere credenciales de GCP. Lee/escribe JSONs en directorios temporales.
"""

import json
import tempfile
from pathlib import Path


from src.phase4_ensamblador.ensamblador import (
    run_ensamblador,
    _calcular_decision_global,
    _verificaciones_cruzadas,
    _detectar_modulos,
    _ensamblar_campos,
)
from src.phase4_ensamblador.schema import CAMPOS_POR_MODULO


# ──────────────────────────────────────────────────────────
# Fixtures de identidad mínima
# ──────────────────────────────────────────────────────────

def _identidad_auto(doc_id: str = "test") -> dict:
    """JSON mínimo de resultado_identidad_cabecera.json con todos los campos en AUTO."""
    def campo_auto(valor: str, fuente: str = "document_ai_nativo") -> dict:
        return {
            "valor_final": valor,
            "fuente_final": fuente,
            "confianza_final": 0.97,
            "decision": "auto",
            "motivo": "Test campo auto",
            "validaciones": {"llm_usado": False},
            "candidatos": [],
        }
    return {
        "documento_id": doc_id,
        "fase": "3_identidad_cabecera",
        "version_politica": "v1",
        "timestamp": "2026-03-25T20:00:00",
        "campos": {
            "nif_entidad":      campo_auto("B12345674"),
            "nombre_entidad":   campo_auto("PROVEEDOR EJEMPLO SL"),
            "numero_factura":   campo_auto("2026-A-0042"),
            "fecha_expedicion": campo_auto("2026-03-10"),
            "fecha_operacion":  campo_auto("2026-03-25", "sistema"),
            "nif_receptor":     campo_auto("A98765432"),
            "nombre_receptor":  campo_auto("CLIENTE RECEPTOR SL"),
        },
        "decision_global": "auto",
        "requiere_revision_humana": False,
        "motivos_revision": [],
        "llm_usado": False,
        "tokens_llm": 0,
    }


# ──────────────────────────────────────────────────────────
# test_decision_global (unit test puro)
# ──────────────────────────────────────────────────────────

def test_decision_global_block_gana_sobre_warn():
    campos = {
        "nif_entidad":    {"decision": "block"},
        "nombre_entidad": {"decision": "warn"},
        "numero_factura": {"decision": "auto"},
        "fecha_expedicion": {"decision": "auto"},
        "total_euros":    {"decision": "pendiente"},
        "nif_receptor":    {"decision": "pendiente"},
        "concepto":       {"decision": "pendiente"},
        "nif_cliente":    {"decision": "pendiente"},
        "nombre_cliente": {"decision": "pendiente"},
    }
    decision, autocargable, _ = _calcular_decision_global(campos)
    assert decision == "block"
    assert not autocargable


def test_decision_global_warn_gana_sobre_pendiente():
    campos = {
        "nif_entidad":    {"decision": "warn"},
        "nombre_entidad": {"decision": "auto"},
        "numero_factura": {"decision": "auto"},
        "fecha_expedicion": {"decision": "auto"},
        "total_euros":    {"decision": "pendiente"},
        "nif_receptor":    {"decision": "pendiente"},
        "concepto":       {"decision": "pendiente"},
        "nif_cliente":    {"decision": "pendiente"},
        "nombre_cliente": {"decision": "pendiente"},
    }
    decision, autocargable, _ = _calcular_decision_global(campos)
    assert decision == "warn"
    assert not autocargable


def test_decision_global_pendiente_cuando_modulos_faltan():
    campos = {
        "nif_entidad":    {"decision": "auto"},
        "nombre_entidad": {"decision": "auto"},
        "numero_factura": {"decision": "auto"},
        "fecha_expedicion": {"decision": "auto"},
        "total_euros":    {"decision": "pendiente"},
        "nif_receptor":    {"decision": "pendiente"},
        "concepto":       {"decision": "pendiente"},
        "nif_cliente":    {"decision": "pendiente"},
        "nombre_cliente": {"decision": "pendiente"},
    }
    decision, autocargable, _ = _calcular_decision_global(campos)
    assert decision == "pendiente"
    assert not autocargable


def test_decision_global_auto_solo_si_todos_obligatorios_auto():
    campos = {campo: {"decision": "auto"} for campo in [
        "nif_entidad", "nombre_entidad", "numero_factura",
        "fecha_expedicion", "total_euros", "nif_receptor", "concepto",
        "nif_cliente", "nombre_cliente"
    ]}
    decision, autocargable, motivos = _calcular_decision_global(campos)
    assert decision == "auto"
    assert autocargable
    assert motivos == []


# ──────────────────────────────────────────────────────────
# test_detectar_modulos
# ──────────────────────────────────────────────────────────

class TestDetectarModulos:
    """Tests para la detección de módulos en disco."""

    def test_no_artifacts_all_pendiente(self, tmp_path):
        """Sin artefactos, todos los módulos quedan pendiente."""
        modulos = _detectar_modulos(tmp_path)
        for _nombre, info in modulos.items():
            assert info["estado"] == "pendiente"
            assert info["data"] is None

    def test_only_ocr_artifact(self, tmp_path):
        """Con solo documento_extraido.json, OCR es ok y el resto pendiente."""
        (tmp_path / "documento_extraido.json").write_text(
            '{"documento_id": "test"}', encoding="utf-8"
        )
        modulos = _detectar_modulos(tmp_path)
        assert modulos["ocr"]["estado"] == "ok"
        assert modulos["ocr"]["data"]["documento_id"] == "test"
        assert modulos["identidad_cabecera"]["estado"] == "pendiente"

    def test_identidad_artifact_present(self, tmp_path):
        """Con artefacto de identidad, módulo identidad queda ok."""
        identidad_data = {
            "documento_id": "test",
            "campos": {
                "nif_entidad": {
                    "valor_final": "B12345674",
                    "fuente_final": "document_ai_nativo",
                    "confianza_final": 0.97,
                    "decision": "auto",
                    "motivo": "Checksum OK"
                }
            },
            "decision_global": "auto"
        }
        (tmp_path / "resultado_identidad_cabecera.json").write_text(
            json.dumps(identidad_data), encoding="utf-8"
        )
        modulos = _detectar_modulos(tmp_path)
        assert modulos["identidad_cabecera"]["estado"] == "ok"
        assert modulos["identidad_cabecera"]["data"]["documento_id"] == "test"

    def test_malformed_json_marks_error(self, tmp_path):
        """JSON malformado marca el módulo como error."""
        (tmp_path / "resultado_identidad_cabecera.json").write_text(
            "{bad json", encoding="utf-8"
        )
        modulos = _detectar_modulos(tmp_path)
        assert modulos["identidad_cabecera"]["estado"] == "error"
        assert modulos["identidad_cabecera"]["data"] is None


# ──────────────────────────────────────────────────────────
# test_ensamblar_campos
# ──────────────────────────────────────────────────────────

class TestEnsamblarCampos:
    """Tests para el ensamblado de campos."""

    def test_all_pendiente_when_no_modules(self):
        """Sin módulos ejecutados, todos los campos quedan pendiente."""
        modulos = {
            "ocr": {"estado": "pendiente", "data": None, "artefacto": None},
            "identidad_cabecera": {"estado": "pendiente", "data": None, "artefacto": None},
            "fiscal": {"estado": "pendiente", "data": None, "artefacto": None},
            "semantica": {"estado": "pendiente", "data": None, "artefacto": None},
            "cliente_destino": {"estado": "pendiente", "data": None, "artefacto": None},
        }
        campos = _ensamblar_campos(modulos)
        for _campo_name, campo_data in campos.items():
            assert campo_data["decision"] == "pendiente"
            assert campo_data["valor_final"] is None
            assert campo_data["fuente_modulo"] == "pendiente"

    def test_identidad_fields_extracted(self):
        """Campos de identidad se extraen correctamente del artefacto."""
        modulos = {
            "ocr": {"estado": "ok", "data": {}, "artefacto": "documento_extraido.json"},
            "identidad_cabecera": {
                "estado": "ok",
                "data": {
                    "campos": {
                        "nif_entidad": {
                            "valor_final": "B12345674",
                            "fuente_final": "document_ai_nativo",
                            "confianza_final": 0.97,
                            "decision": "auto",
                            "motivo": "Checksum OK",
                            "candidatos": [],
                        },
                        "nombre_entidad": {
                            "valor_final": "EMPRESA TEST SL",
                            "fuente_final": "document_ai_nativo",
                            "confianza_final": 0.88,
                            "decision": "warn",
                            "motivo": "Baja confianza",
                            "candidatos": [],
                        },
                        "numero_factura": {
                            "valor_final": "F-2026-001",
                            "fuente_final": "document_ai_nativo",
                            "confianza_final": 0.95,
                            "decision": "auto",
                            "motivo": "OK",
                            "candidatos": [],
                        },
                        "fecha_expedicion": {
                            "valor_final": "2026-03-10",
                            "fuente_final": "normalized_value",
                            "confianza_final": 0.99,
                            "decision": "auto",
                            "motivo": "ISO 8601",
                            "candidatos": [],
                        },
                        "fecha_operacion": {
                            "valor_final": "2026-03-25",
                            "fuente_final": "sistema",
                            "confianza_final": 1.0,
                            "decision": "auto",
                            "motivo": "Fecha del sistema",
                            "candidatos": [],
                        },
                    }
                },
                "artefacto": "resultado_identidad_cabecera.json",
            },
            "fiscal": {"estado": "pendiente", "data": None, "artefacto": None},
            "semantica": {"estado": "pendiente", "data": None, "artefacto": None},
            "cliente_destino": {"estado": "pendiente", "data": None, "artefacto": None},
        }
        campos = _ensamblar_campos(modulos)

        # Campos de identidad resueltos
        assert campos["nif_entidad"]["valor_final"] == "B12345674"
        assert campos["nif_entidad"]["fuente_modulo"] == "identidad_cabecera"
        assert campos["nif_entidad"]["fuente_dato"] == "document_ai_nativo"
        assert campos["nif_entidad"]["confianza"] == 0.97
        assert campos["nif_entidad"]["decision"] == "auto"

        assert campos["nombre_entidad"]["valor_final"] == "EMPRESA TEST SL"
        assert campos["nombre_entidad"]["decision"] == "warn"

        # Campos pendientes de otros módulos
        assert campos["total_euros"]["decision"] == "pendiente"
        assert campos["concepto"]["decision"] == "pendiente"
        assert campos["nif_receptor"]["decision"] == "pendiente"


# ──────────────────────────────────────────────────────────
# test error vs pendiente
# ──────────────────────────────────────────────────────────

class TestErrorVsPendiente:
    """Tests para la distinción error vs pendiente en campos."""

    def test_error_module_produces_block_fields(self):
        """Módulo en error produce campos con decisión block, no pendiente."""
        modulos = {
            "ocr": {"estado": "ok", "data": {}, "artefacto": "documento_extraido.json"},
            "identidad_cabecera": {"estado": "error", "data": None, "artefacto": "resultado_identidad_cabecera.json"},
            "fiscal": {"estado": "pendiente", "data": None, "artefacto": None},
            "semantica": {"estado": "pendiente", "data": None, "artefacto": None},
            "cliente_destino": {"estado": "pendiente", "data": None, "artefacto": None},
        }
        campos = _ensamblar_campos(modulos)

        # Campos de identidad deben ser block (módulo falló)
        assert campos["nif_entidad"]["decision"] == "block"
        assert campos["nif_entidad"]["fuente_modulo"] == "error"

        # Campos de fiscal deben ser pendiente (módulo no ejecutado)
        assert campos["total_euros"]["decision"] == "pendiente"
        assert campos["total_euros"]["fuente_modulo"] == "pendiente"

    def test_error_module_produces_block_decision_global(self):
        """decision_global debe ser block si hay módulo en error con campos obligatorios."""
        campos = {}
        for campo_name in CAMPOS_POR_MODULO:
            campos[campo_name] = {
                "valor_final": None,
                "decision": "pendiente",
                "motivo": "Pendiente",
            }
        for campo_name in ("nif_entidad", "nombre_entidad", "numero_factura", "fecha_expedicion"):
            campos[campo_name]["decision"] = "block"
            campos[campo_name]["motivo"] = "Módulo identidad_cabecera falló"

        decision, autocargable, motivos = _calcular_decision_global(campos)
        assert decision == "block"
        assert autocargable is False


# ──────────────────────────────────────────────────────────
# test_ensamblador_solo_identidad (integración)
# ──────────────────────────────────────────────────────────

def test_ensamblador_solo_identidad_campos_pendientes():
    """
    Solo identidad presente. Campos fiscales/semánticos/cliente quedan "pendiente".
    decision_global debe ser "pendiente" (hay campos obligatorios pendientes).
    """
    with tempfile.TemporaryDirectory() as tmp:
        identidad = _identidad_auto()
        (Path(tmp) / "resultado_identidad_cabecera.json").write_text(
            json.dumps(identidad), encoding="utf-8"
        )
        (Path(tmp) / "documento_extraido.json").write_text("{}", encoding="utf-8")

        ok = run_ensamblador("test", tmp, "20_COMPRAS_GASTOS")
        assert ok

        with open(Path(tmp) / "resultado_validacion.json", encoding="utf-8") as f:
            resultado = json.load(f)

        assert resultado["decision_global"] == "pendiente"
        assert not resultado["autocargable"]
        assert resultado["modulos"]["fiscal"]["estado"] == "pendiente"
        assert resultado["modulos"]["semantica"]["estado"] == "pendiente"
        assert resultado["modulos"]["cliente_destino"]["estado"] == "pendiente"
        assert resultado["campos"]["total_euros"]["decision"] == "pendiente"
        assert resultado["campos"]["nif_entidad"]["decision"] == "auto"


def test_ensamblador_solo_identidad_campos_identidad_ok():
    """Los campos de identidad deben aparecer correctamente ensamblados."""
    with tempfile.TemporaryDirectory() as tmp:
        identidad = _identidad_auto()
        (Path(tmp) / "resultado_identidad_cabecera.json").write_text(
            json.dumps(identidad), encoding="utf-8"
        )
        (Path(tmp) / "documento_extraido.json").write_text("{}", encoding="utf-8")

        run_ensamblador("test", tmp, "20_COMPRAS_GASTOS")

        with open(Path(tmp) / "resultado_validacion.json", encoding="utf-8") as f:
            resultado = json.load(f)

        nif = resultado["campos"]["nif_entidad"]
        assert nif["valor_final"] == "B12345674"
        assert nif["fuente_modulo"] == "identidad_cabecera"
        assert nif["decision"] == "auto"

        fecha_oper = resultado["campos"]["fecha_operacion"]
        assert fecha_oper["valor_final"] == "2026-03-25"


# ──────────────────────────────────────────────────────────
# test_ensamblador_modulo_error (integración)
# ──────────────────────────────────────────────────────────

def test_ensamblador_sin_identidad_modulo_pendiente():
    """Sin artefacto de identidad, el módulo queda como "pendiente"."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "documento_extraido.json").write_text("{}", encoding="utf-8")

        ok = run_ensamblador("test_sin_identidad", tmp, "20_COMPRAS_GASTOS")
        assert ok

        with open(Path(tmp) / "resultado_validacion.json", encoding="utf-8") as f:
            resultado = json.load(f)

        assert resultado["modulos"]["identidad_cabecera"]["estado"] == "pendiente"
        assert resultado["campos"]["nif_entidad"]["decision"] == "pendiente"
        assert resultado["decision_global"] == "pendiente"


def test_ensamblador_identidad_json_corrupto_marca_error():
    """Si el JSON de identidad está corrupto, el módulo se marca como 'error'."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "resultado_identidad_cabecera.json").write_text(
            "corrupto-no-json", encoding="utf-8"
        )
        (Path(tmp) / "documento_extraido.json").write_text("{}", encoding="utf-8")

        ok = run_ensamblador("test_corrupto", tmp, "20_COMPRAS_GASTOS")
        assert ok

        with open(Path(tmp) / "resultado_validacion.json", encoding="utf-8") as f:
            resultado = json.load(f)

        assert resultado["modulos"]["identidad_cabecera"]["estado"] == "error"
        assert resultado["campos"]["nif_entidad"]["decision"] == "block"


# ──────────────────────────────────────────────────────────
# test_verificaciones_cruzadas
# ──────────────────────────────────────────────────────────

def test_verificacion_fecha_no_futura_true():
    """fecha_expedicion <= fecha_operacion → True."""
    campos = {
        "fecha_expedicion": {"valor_final": "2026-03-10", "decision": "auto"},
        "fecha_operacion":  {"valor_final": "2026-03-25", "decision": "auto"},
        "nif_entidad":      {"valor_final": "B12345674",  "decision": "auto"},
    }
    modulos = {"identidad_cabecera": {"estado": "ok"}}
    v = _verificaciones_cruzadas(campos, modulos)
    assert v["fecha_expedicion_no_futura"] is True


def test_verificacion_fecha_futura_false():
    """fecha_expedicion > fecha_operacion → False."""
    campos = {
        "fecha_expedicion": {"valor_final": "2026-12-31", "decision": "auto"},
        "fecha_operacion":  {"valor_final": "2026-03-25", "decision": "auto"},
        "nif_entidad":      {"valor_final": "B12345674",  "decision": "auto"},
    }
    modulos = {"identidad_cabecera": {"estado": "ok"}}
    v = _verificaciones_cruzadas(campos, modulos)
    assert v["fecha_expedicion_no_futura"] is False


def test_verificacion_fecha_null_si_identidad_pendiente():
    """Si identidad está pendiente, fecha_expedicion_no_futura debe ser null."""
    campos = {}
    modulos = {"identidad_cabecera": {"estado": "pendiente"}}
    v = _verificaciones_cruzadas(campos, modulos)
    assert v["fecha_expedicion_no_futura"] is None


def test_verificacion_suma_fiscal_null_sin_fiscal():
    """Sin módulo fiscal, suma_fiscal_correcta debe ser null."""
    campos = {}
    modulos = {"identidad_cabecera": {"estado": "ok"}, "fiscal": {"estado": "pendiente"}}
    v = _verificaciones_cruzadas(campos, modulos)
    assert v["suma_fiscal_correcta"] is None


# ──────────────────────────────────────────────────────────
# test schema_version y estructura del resultado
# ──────────────────────────────────────────────────────────

def test_resultado_validacion_estructura_completa():
    """El JSON de salida debe tener todos los bloques definidos en la spec."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "documento_extraido.json").write_text("{}", encoding="utf-8")
        run_ensamblador("doc_estructura", tmp, "21_VENTAS_INGRESOS")

        with open(Path(tmp) / "resultado_validacion.json", encoding="utf-8") as f:
            resultado = json.load(f)

        for clave in ["documento_id", "schema_version", "fecha_ensamblado",
                      "libro", "modulos", "campos", "decision_global",
                      "autocargable", "motivos_revision", "verificaciones"]:
            assert clave in resultado, f"Clave faltante: {clave}"

        assert resultado["schema_version"] == "v1"
        assert resultado["libro"] == "21_VENTAS_INGRESOS"
        assert resultado["documento_id"] == "doc_estructura"
        assert "ocr" in resultado["modulos"]

        for campo in CAMPOS_POR_MODULO:
            assert campo in resultado["campos"], f"Campo faltante en resultado: {campo}"


# ──────────────────────────────────────────────────────────
# test_schema (registros declarativos)
# ──────────────────────────────────────────────────────────

class TestSchema:
    """Tests de los registros declarativos del schema."""

    def test_every_campo_maps_to_known_modulo(self):
        from src.phase4_ensamblador.schema import MODULOS_FASE3
        for _campo, modulo in CAMPOS_POR_MODULO.items():
            assert modulo in MODULOS_FASE3

    def test_campos_obligatorios_subset_of_campos_por_modulo(self):
        from src.phase4_ensamblador.schema import CAMPOS_OBLIGATORIOS
        for campo in CAMPOS_OBLIGATORIOS:
            assert campo in CAMPOS_POR_MODULO

    def test_campo_pendiente_has_required_keys(self):
        from src.phase4_ensamblador.schema import CAMPO_PENDIENTE
        expected_keys = {
            "valor_final", "fuente_modulo", "fuente_dato",
            "confianza", "decision", "llm_usado", "motivo", "ocr_fallback"
        }
        assert set(CAMPO_PENDIENTE.keys()) == expected_keys

    def test_campo_pendiente_motivo_has_placeholder(self):
        from src.phase4_ensamblador.schema import CAMPO_PENDIENTE
        assert "{modulo}" in CAMPO_PENDIENTE["motivo"]

    def test_campo_error_has_block_decision(self):
        from src.phase4_ensamblador.schema import CAMPO_ERROR
        assert CAMPO_ERROR["decision"] == "block"
        assert "{modulo}" in CAMPO_ERROR["motivo"]
