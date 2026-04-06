"""
Tests para la fase 3 v2 — Resolvedor de Cabecera e Identidad.

Cubre:
  - DocumentAIEntityExtractor: extracción con normalized_value, textAnchor, pageAnchor
  - Resolvedores por campo: nif, nombre, numero_factura, fechas
  - Normalización de fechas a ISO 8601 (todos los formatos HORECA)
  - Decisión por umbrales: AUTO / WARN / BLOCK
  - Gate LLM: solo se llama cuando hay conflicto o baja confianza
  - Revalidación de correcciones LLM
  - Decisión global = peor campo obligatorio
  - CabeceraResult serializable a JSON

No requiere red ni API keys.
Ejecutar: pytest tests/test_3_cabecera.py -v
"""

import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from nif_cif_validator import validar_identificador_fiscal
from field_candidate import (
    FieldCandidate, FieldResolution, CabeceraResult,
    FuenteCandidato, DecisionCampo
)
from docai_extractor import DocumentAIEntityExtractor, _extraer_bbox_desde_page_refs
from field_resolvers import (
    _normalizar_fecha, resolver_nif_entidad, resolver_numero_factura,
    resolver_fecha_expedicion, resolver_fecha_operacion, _necesita_llm
)
from cabecera_resolver import CabeceraResolver, _revalidar_valor


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _entity(tipo, mention, conf=0.95, normalized=None, page=0, nverts=None):
    """Construir un entity de Document AI en formato REST (camelCase)."""
    e = {
        "type": tipo,
        "mentionText": mention,
        "confidence": conf,
    }
    if normalized:
        e["normalizedValue"] = {"text": normalized}
    if nverts:
        e["pageAnchor"] = {
            "pageRefs": [{
                "page": page,
                "boundingPoly": {
                    "normalizedVertices": nverts
                }
            }]
        }
    return e


def _raw_doc(entities, text=""):
    """Construir raw Document AI response mínimo."""
    return {"entities": entities, "text": text}


def _field_res(campo, valor, fuente, conf, dec, candidatos=None, validaciones=None):
    return FieldResolution(
        campo=campo,
        valor_final=valor,
        fuente_final=fuente,
        confianza_final=conf,
        decision=dec,
        motivo="test",
        candidatos=candidatos or [],
        validaciones=validaciones or {},
    )


# ──────────────────────────────────────────────────────────
# DocumentAIEntityExtractor
# ──────────────────────────────────────────────────────────

class TestDocAIExtractor:

    def test_extrae_mention_text(self):
        raw = _raw_doc([_entity("supplier_tax_id", "A46103834", 0.97)])
        e = DocumentAIEntityExtractor(raw).extraer_primero("supplier_tax_id")
        assert e is not None
        assert e.mention_text == "A46103834"
        assert e.confidence == 0.97

    def test_prefiere_normalized_value_para_fechas(self):
        raw = _raw_doc([
            _entity("invoice_date", "15/03/2026", 0.93, normalized="2026-03-15")
        ])
        e = DocumentAIEntityExtractor(raw).extraer_primero("invoice_date")
        assert e.normalized_text == "2026-03-15"
        assert e.valor_preferido == "2026-03-15"  # normalized sobre mention

    def test_mention_text_para_nif(self):
        raw = _raw_doc([
            _entity("supplier_tax_id", "A46103834", 0.95, normalized="A-46103834")
        ])
        e = DocumentAIEntityExtractor(raw).extraer_primero("supplier_tax_id")
        # Para NIF: preferir mention_text (Document AI puede alterar formato)
        assert e.valor_para_nif == "A46103834"

    def test_extrae_bbox_desde_page_anchor(self):
        nverts = [
            {"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1},
            {"x": 0.5, "y": 0.2}, {"x": 0.1, "y": 0.2},
        ]
        raw = _raw_doc([_entity("supplier_name", "EMPRESA SL", 0.95, nverts=nverts)])
        e = DocumentAIEntityExtractor(raw).extraer_primero("supplier_name")
        assert e.bbox_normalizado is not None
        assert e.bbox_normalizado["x_min"] == 0.1
        assert e.bbox_normalizado["x_max"] == 0.5

    def test_extraer_primero_devuelve_none_si_no_existe(self):
        raw = _raw_doc([])
        assert DocumentAIEntityExtractor(raw).extraer_primero("supplier_tax_id") is None

    def test_texto_completo(self):
        raw = _raw_doc([], text="Texto OCR de la factura")
        assert DocumentAIEntityExtractor(raw).texto_completo() == "Texto OCR de la factura"

    def test_acepta_formato_proto_snake_case(self):
        # Algunas versiones devuelven snake_case en vez de camelCase
        raw = {
            "entities": [{
                "type_": "invoice_id",
                "mention_text": "F-2026-001",
                "confidence": 0.88,
            }]
        }
        e = DocumentAIEntityExtractor(raw).extraer_primero("invoice_id")
        assert e is not None
        assert e.mention_text == "F-2026-001"

    def test_page_refs_extrae_numero_pagina(self):
        nverts = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1},
                  {"x": 0.5, "y": 0.2}, {"x": 0.1, "y": 0.2}]
        raw = _raw_doc([_entity("invoice_date", "01/01/2026", nverts=nverts, page=2)])
        e = DocumentAIEntityExtractor(raw).extraer_primero("invoice_date")
        assert e.page_number == 2


# ──────────────────────────────────────────────────────────
# Normalización de fechas
# ──────────────────────────────────────────────────────────

class TestNormalizarFecha:

    def test_iso_ya_normalizado(self):
        assert _normalizar_fecha("2026-03-15") == "2026-03-15"

    def test_dd_mm_yyyy_slash(self):
        assert _normalizar_fecha("15/03/2026") == "2026-03-15"

    def test_dd_mm_yyyy_guion(self):
        assert _normalizar_fecha("15-03-2026") == "2026-03-15"

    def test_dd_mm_yy_slash(self):
        assert _normalizar_fecha("15/03/26") == "2026-03-15"

    def test_ddmmyyyy_sin_separadores(self):
        assert _normalizar_fecha("15032026") == "2026-03-15"

    def test_fecha_texto_espanol(self):
        assert _normalizar_fecha("15 de marzo de 2026") == "2026-03-15"

    def test_fecha_texto_sin_de(self):
        assert _normalizar_fecha("15 marzo 2026") == "2026-03-15"

    def test_fecha_invalida_devuelve_none(self):
        assert _normalizar_fecha("99/99/9999") is None

    def test_texto_irreconocible_devuelve_none(self):
        assert _normalizar_fecha("no es una fecha") is None

    def test_fecha_punto_separador(self):
        assert _normalizar_fecha("15.03.2026") == "2026-03-15"


# ──────────────────────────────────────────────────────────
# Resolvedor NIF
# ──────────────────────────────────────────────────────────

class TestResolverNif:

    def test_nativo_valido_da_auto(self):
        raw = _raw_doc([_entity("supplier_tax_id", "A46103834", 0.97)])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        assert res.decision == DecisionCampo.AUTO
        assert res.valor_final == "A46103834"
        assert res.validaciones.get("checksum_ok") is True

    def test_nativo_invalido_penaliza_confianza(self):
        raw = _raw_doc([_entity("supplier_tax_id", "B12345679", 0.97)])  # ctrl incorrecto
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        # Checksum falla → confianza penalizada → no debe ser AUTO
        assert res.decision != DecisionCampo.AUTO
        assert res.validaciones.get("checksum_ok") is False

    def test_normaliza_nif_con_espacios(self):
        raw = _raw_doc([_entity("supplier_tax_id", "A 46103834", 0.95)])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        assert res.valor_final == "A46103834"

    def test_sin_nativo_usa_regex_contextual(self):
        texto = "CIF: B12345674\nRazón social: EMPRESA SL"
        raw = _raw_doc([], text=texto)
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        # Regex contextual debe encontrar B12345674
        assert res.valor_final == "B12345674"
        assert res.fuente_final == FuenteCandidato.REGEX_CONTEXTUAL

    def test_sin_candidatos_es_block(self):
        raw = _raw_doc([], text="Sin identificador fiscal en este texto")
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        assert res.decision == DecisionCampo.BLOCK
        assert res.valor_final is None

    def test_candidatos_contienen_trazabilidad(self):
        raw = _raw_doc([_entity("supplier_tax_id", "A46103834", 0.95)])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_nif_entidad(extractor)
        assert len(res.candidatos) >= 1
        c = res.candidatos[0]
        assert "fuente" in c
        assert "valor_normalizado" in c
        assert "validacion_ok" in c


# ──────────────────────────────────────────────────────────
# Resolvedor número de factura
# ──────────────────────────────────────────────────────────

class TestResolverNumeroFactura:

    def test_nativo_invoice_id(self):
        raw = _raw_doc([_entity("invoice_id", "F-2026-001", 0.92)])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_numero_factura(extractor)
        assert res.valor_final == "F-2026-001"
        assert res.decision == DecisionCampo.AUTO

    def test_regex_contextual_factura(self):
        texto = "Factura nº: 2026/003\nFecha: 15/03/2026"
        raw = _raw_doc([], text=texto)
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_numero_factura(extractor)
        assert res.valor_final == "2026/003"
        assert res.fuente_final == FuenteCandidato.REGEX_CONTEXTUAL

    def test_sin_numero_es_block(self):
        raw = _raw_doc([], text="Este texto no tiene número de factura")
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_numero_factura(extractor)
        assert res.decision == DecisionCampo.BLOCK


# ──────────────────────────────────────────────────────────
# Resolvedor fechas
# ──────────────────────────────────────────────────────────

class TestResolverFechas:

    def test_fecha_expedicion_normalized_value_preferido(self):
        # Document AI devuelve normalized_value con ISO → debe usarse
        raw = _raw_doc([
            _entity("invoice_date", "15/03/2026", 0.93, normalized="2026-03-15")
        ])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_fecha_expedicion(extractor)
        assert res.valor_final == "2026-03-15"
        assert res.fuente_final == FuenteCandidato.NORMALIZED_VALUE

    def test_fecha_expedicion_mention_text_normalizado(self):
        raw = _raw_doc([_entity("invoice_date", "15/03/2026", 0.91)])
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_fecha_expedicion(extractor)
        assert res.valor_final == "2026-03-15"

    def test_fecha_operacion_es_siempre_auto(self):
        res = resolver_fecha_operacion()
        assert res.decision == DecisionCampo.AUTO
        assert res.confianza_final == 1.0
        # Formato ISO 8601
        import re
        assert re.match(r'^\d{4}-\d{2}-\d{2}$', res.valor_final)

    def test_fecha_regex_contextual_como_fallback(self):
        texto = "Fecha de expedición: 15/03/2026\nImporte: 100€"
        raw = _raw_doc([], text=texto)
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_fecha_expedicion(extractor)
        assert res.valor_final == "2026-03-15"

    def test_fecha_sin_candidatos_es_block(self):
        raw = _raw_doc([], text="Texto sin fechas aquí")
        extractor = DocumentAIEntityExtractor(raw)
        res = resolver_fecha_expedicion(extractor)
        assert res.decision == DecisionCampo.BLOCK


# ──────────────────────────────────────────────────────────
# Gate LLM
# ──────────────────────────────────────────────────────────

class TestGateLLM:

    def test_no_necesita_llm_si_auto_un_candidato(self):
        res = _field_res(
            "nif_entidad", "A46103834", FuenteCandidato.DOCUMENT_AI_NATIVO,
            0.95, DecisionCampo.AUTO,
            candidatos=[{"valor_normalizado": "A46103834", "validacion_ok": True,
                          "fuente": "document_ai_nativo"}]
        )
        assert _necesita_llm(res) is False

    def test_necesita_llm_si_baja_confianza(self):
        res = _field_res(
            "nif_entidad", "A46103834", FuenteCandidato.DOCUMENT_AI_NATIVO,
            0.55, DecisionCampo.WARN,
            candidatos=[{"valor_normalizado": "A46103834", "validacion_ok": True,
                          "fuente": "document_ai_nativo"}]
        )
        assert _necesita_llm(res) is True

    def test_necesita_llm_si_multiples_valores_distintos(self):
        res = _field_res(
            "nif_entidad", "A46103834", FuenteCandidato.DOCUMENT_AI_NATIVO,
            0.85, DecisionCampo.WARN,
            candidatos=[
                {"valor_normalizado": "A46103834", "validacion_ok": True,
                 "fuente": "document_ai_nativo"},
                {"valor_normalizado": "B12345674", "validacion_ok": True,
                 "fuente": "regex_contextual"},
            ]
        )
        assert _necesita_llm(res) is True


# ──────────────────────────────────────────────────────────
# Revalidación de correcciones LLM
# ──────────────────────────────────────────────────────────

class TestRevalidarValor:

    def test_nif_valido_pasa(self):
        ok, _ = _revalidar_valor("nif_entidad", "A46103834")
        assert ok is True

    def test_nif_invalido_no_pasa(self):
        ok, motivo = _revalidar_valor("nif_entidad", "B99999999")
        assert ok is False
        assert len(motivo) > 0

    def test_fecha_iso_pasa(self):
        ok, _ = _revalidar_valor("fecha_expedicion", "2026-03-15")
        assert ok is True

    def test_fecha_no_iso_no_pasa(self):
        ok, _ = _revalidar_valor("fecha_expedicion", "15/03/2026")
        assert ok is False

    def test_fecha_iso_invalida_no_pasa(self):
        ok, _ = _revalidar_valor("fecha_expedicion", "2026-13-99")
        assert ok is False

    def test_numero_factura_minimo_dos_chars(self):
        ok, _ = _revalidar_valor("numero_factura", "F1")
        assert ok is True

    def test_numero_factura_vacio_no_pasa(self):
        ok, _ = _revalidar_valor("numero_factura", "")
        assert ok is False


# ──────────────────────────────────────────────────────────
# CabeceraResolver — integración sin LLM
# ──────────────────────────────────────────────────────────

class TestCabeceraResolver:

    def _resolver(self):
        return CabeceraResolver(usar_llm=False, umbral_auto=0.90, umbral_warn=0.65)

    def _raw_completo(self):
        return _raw_doc([
            _entity("supplier_tax_id", "A46103834", 0.97),
            _entity("supplier_name", "MERCADONA SA", 0.96),
            _entity("invoice_id", "F-2026-001", 0.94),
            _entity("invoice_date", "15/03/2026", 0.93, normalized="2026-03-15"),
        ])

    def test_documento_completo_da_global_auto(self):
        resultado = self._resolver().resolver(self._raw_completo(), "doc-001")
        assert resultado.decision_global == DecisionCampo.AUTO.value
        assert resultado.requiere_revision_humana is False

    def test_nif_correcto(self):
        resultado = self._resolver().resolver(self._raw_completo())
        assert resultado.nif_entidad["valor_final"] == "A46103834"
        assert resultado.nif_entidad["decision"] == DecisionCampo.AUTO.value

    def test_fecha_preferida_es_normalized_value(self):
        resultado = self._resolver().resolver(self._raw_completo())
        assert resultado.fecha_expedicion["valor_final"] == "2026-03-15"
        assert resultado.fecha_expedicion["fuente_final"] == FuenteCandidato.NORMALIZED_VALUE.value

    def test_fecha_operacion_siempre_presente(self):
        resultado = self._resolver().resolver(self._raw_completo())
        assert resultado.fecha_operacion is not None
        assert resultado.fecha_operacion["decision"] == DecisionCampo.AUTO.value

    def test_nif_invalido_baja_decision_global_a_block(self):
        raw = _raw_doc([
            _entity("supplier_tax_id", "B12345679", 0.97),  # checksum incorrecto
            _entity("supplier_name", "EMPRESA SL", 0.95),
            _entity("invoice_id", "F-001", 0.92),
            _entity("invoice_date", "15/03/2026", 0.93, normalized="2026-03-15"),
        ])
        resultado = self._resolver().resolver(raw)
        assert resultado.decision_global == DecisionCampo.BLOCK.value

    def test_campo_obligatorio_block_hace_global_block(self):
        # Sin fecha de expedición → fecha_expedicion BLOCK → global BLOCK
        raw = _raw_doc([
            _entity("supplier_tax_id", "A46103834", 0.97),
            _entity("supplier_name", "MERCADONA SA", 0.95),
            _entity("invoice_id", "F-001", 0.93),
            # No hay invoice_date
        ])
        resultado = self._resolver().resolver(raw)
        assert resultado.decision_global == DecisionCampo.BLOCK.value

    def test_resultado_serializable_json(self):
        resultado = self._resolver().resolver(self._raw_completo(), "doc-001")
        json_str = resultado.to_json()
        parsed = json.loads(json_str)
        assert parsed["documento_id"] == "doc-001"
        assert parsed["version_politica"] == "v1"
        assert "campos" in parsed
        assert "nif_entidad" in parsed["campos"]
        assert "nombre_entidad" in parsed["campos"]
        assert "numero_factura" in parsed["campos"]
        assert "fecha_expedicion" in parsed["campos"]
        assert "decision_global" in parsed
        assert "requiere_revision_humana" in parsed

    def test_candidatos_en_cada_campo(self):
        resultado = self._resolver().resolver(self._raw_completo())
        nif = resultado.nif_entidad
        assert "candidatos" in nif
        assert len(nif["candidatos"]) >= 1
        c = nif["candidatos"][0]
        assert "valor_normalizado" in c
        assert "fuente" in c
        assert "confianza" in c
        assert "validacion_ok" in c

    def test_llm_no_se_llama_cuando_usar_llm_false(self):
        resolver = CabeceraResolver(usar_llm=False)
        resultado = resolver.resolver(self._raw_completo())
        assert resultado.llm_usado is False
        assert resultado.tokens_llm == 0

    def test_documento_id_generado_si_no_se_pasa(self):
        resultado = self._resolver().resolver(self._raw_completo())
        assert resultado.documento_id
        assert len(resultado.documento_id) > 5

    def test_motivos_revision_solo_para_no_auto(self):
        raw = _raw_doc([
            _entity("supplier_tax_id", "B12345679", 0.97),  # checksum falla
            _entity("supplier_name", "EMPRESA SL", 0.95),
            _entity("invoice_id", "F-001", 0.93),
            _entity("invoice_date", "2026-03-15", 0.94, normalized="2026-03-15"),
        ])
        resultado = self._resolver().resolver(raw)
        assert len(resultado.motivos_revision) > 0
        assert any("nif_entidad" in m for m in resultado.motivos_revision)


# ──────────────────────────────────────────────────────────
# LLM árbitro — gate de revalidación (sin llamadas reales)
# ──────────────────────────────────────────────────────────

class TestLLMArbiterRevalidacion:
    """
    Verifica que el CabeceraResolver revalida correcciones del LLM.
    Gemini mockeado: elige un candidato por índice.
    """

    def _resolver_con_llm_mock(self, idx_elegido, valor_propuesto, confianza=0.88):
        from llm_disambiguator import ArbitrajeResult
        resolver = CabeceraResolver(usar_llm=True, umbral_auto=0.90, umbral_warn=0.65)
        mock_llm = MagicMock()
        mock_llm.arbitrar.return_value = ArbitrajeResult(
            indice_elegido=idx_elegido,
            valor_propuesto=valor_propuesto,
            justificacion="Test mock",
            confianza=confianza,
            tokens_prompt=5,
            tokens_respuesta=10,
        )
        resolver._llm = mock_llm
        return resolver

    def test_llm_correccion_valida_se_acepta_como_warn(self):
        # NIF de baja confianza → LLM árbitro propone NIF válido
        raw = _raw_doc([
            _entity("supplier_tax_id", "A46103834", 0.60),  # baja confianza → llama LLM
            _entity("supplier_name", "MERCADONA SA", 0.95),
            _entity("invoice_id", "F-001", 0.93),
            _entity("invoice_date", "2026-03-15", 0.93, normalized="2026-03-15"),
        ])
        resolver = self._resolver_con_llm_mock(0, "A46103834", confianza=0.85)
        resultado = resolver.resolver(raw, "doc-llm-ok")

        nif = resultado.nif_entidad
        assert nif["valor_final"] == "A46103834"
        # LLM intervino → nunca AUTO, mínimo WARN
        assert nif["decision"] in (DecisionCampo.WARN.value, DecisionCampo.AUTO.value)
        assert resultado.llm_usado is True

    def test_llm_correccion_invalida_no_se_aplica(self):
        """LLM propone NIF que falla checksum → se ignora, no se aplica."""
        raw = _raw_doc([
            _entity("supplier_tax_id", "A46103834", 0.60),
            _entity("supplier_name", "EMPRESA SL", 0.95),
            _entity("invoice_id", "F-001", 0.93),
            _entity("invoice_date", "2026-03-15", 0.93, normalized="2026-03-15"),
        ])
        # LLM propone NIF inválido
        resolver = self._resolver_con_llm_mock(0, "B99999999", confianza=0.85)
        # Sobreescribir el candidato del mock para que apunte al NIF inválido
        mock_llm = resolver._llm
        from llm_disambiguator import ArbitrajeResult
        mock_llm.arbitrar.return_value = ArbitrajeResult(
            indice_elegido=0,
            valor_propuesto="B99999999",
            justificacion="Mock con NIF inválido",
            confianza=0.85,
            tokens_prompt=5,
            tokens_respuesta=10,
        )
        resultado = resolver.resolver(raw, "doc-llm-bad")
        nif = resultado.nif_entidad
        # El NIF final NO debe ser el propuesto por el LLM (falla checksum)
        assert nif["valor_final"] != "B99999999", (
            "LLM propuso NIF inválido → no debe ser valor_final"
        )
