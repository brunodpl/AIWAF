"""
Tests unitarios para la fuente FASE2_OCR en los resolvedores de campo.

Verifica que _candidatos_desde_fase2() inyecta correctamente los valores
extraídos por Gemini en fase 2 como candidatos adicionales en los resolvedores.
"""

import pytest

from src.phase3_identidad_cabecera.docai_extractor import DocumentAIEntityExtractor
from src.phase3_identidad_cabecera.field_resolvers import (
    _candidatos_desde_fase2,
    resolver_nif_entidad,
    resolver_nombre_entidad,
    resolver_numero_factura,
    resolver_fecha_expedicion,
    resolver_nif_receptor,
    resolver_nombre_receptor,
)
from src.phase3_identidad_cabecera.field_candidate import (
    DecisionCampo, FuenteCandidato
)


# ── Fixture: extractor vacío (simula bridge file sin entities DocAI) ────────

@pytest.fixture
def extractor_vacio():
    """DocumentAIEntityExtractor con bridge file vacío (sin entities, sin texto)."""
    return DocumentAIEntityExtractor({"entities": [], "text": "", "pages": []})


@pytest.fixture
def extractor_con_texto():
    """DocumentAIEntityExtractor con texto OCR pero sin entities estructuradas."""
    return DocumentAIEntityExtractor({
        "entities": [],
        "text": "CIF B15026693 ESPINA&DELFIN N.FRA: N27253401514 Fecha: 11/02/2026",
        "pages": [],
    })


# ── Tests de _candidatos_desde_fase2 ────────────────────────────────────────

def test_candidatos_desde_fase2_nif_valido():
    """NIF con checksum OK → candidato con conf=0.75, validacion_ok=True."""
    doc = {"identificacion": {"nif_entidad": {"valor": "B15026693", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nif_entidad"]
    assert c is not None
    assert c.fuente == FuenteCandidato.FASE2_OCR
    assert c.confianza == pytest.approx(0.75)
    assert c.validacion_ok is True
    assert c.valor_normalizado == "B15026693"


def test_candidatos_desde_fase2_nif_con_separadores():
    """NIF con separadores (formato Makro "A-28/647451") → limpieza antes de checksum."""
    doc = {"identificacion": {"nif_entidad": {"valor": "A-28/647451", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nif_entidad"]
    assert c is not None
    # El valor se limpia de separadores
    assert "/" not in c.valor_normalizado
    assert "-" not in c.valor_normalizado


def test_candidatos_desde_fase2_nif_invalido_penalizado():
    """NIF con checksum FALLA → penalización: confianza = 0.75 * 0.3 = 0.225."""
    doc = {"identificacion": {"nif_entidad": {"valor": "B9999999", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nif_entidad"]
    assert c is not None
    assert c.confianza == pytest.approx(0.75 * 0.3)
    assert c.validacion_ok is False


def test_candidatos_desde_fase2_nombre():
    """Nombre sin checksum → conf=0.75, validacion_ok=True, normalizado a mayúsculas."""
    doc = {"identificacion": {"nombre_entidad": {"valor": "Makro Distribucion Mayorista, S.A.", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nombre_entidad"]
    assert c is not None
    assert c.fuente == FuenteCandidato.FASE2_OCR
    assert c.confianza == pytest.approx(0.75)
    assert c.valor_normalizado == "MAKRO DISTRIBUCION MAYORISTA, S.A."
    assert c.validacion_ok is True


def test_candidatos_desde_fase2_fecha_iso():
    """Fecha en formato ISO → conf=0.75, valor_normalizado en ISO."""
    doc = {"identificacion": {"fecha_expedicion": {"valor": "2026-02-11", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["fecha_expedicion"]
    assert c is not None
    assert c.confianza == pytest.approx(0.75)
    assert c.valor_normalizado == "2026-02-11"


def test_candidatos_desde_fase2_valores_nulos():
    """Campos con valor null → candidatos None para todos los campos."""
    doc = {"identificacion": {
        "nif_entidad": {"valor": None},
        "nombre_entidad": {"valor": None},
        "numero_factura": {"valor": None},
        "fecha_expedicion": {"valor": None},
    }}
    resultado = _candidatos_desde_fase2(doc)

    assert resultado["nif_entidad"] is None
    assert resultado["nombre_entidad"] is None
    assert resultado["numero_factura"] is None
    assert resultado["fecha_expedicion"] is None


def test_candidatos_desde_fase2_doc_vacio():
    """documento_extraido vacío → todos None."""
    assert all(v is None for v in _candidatos_desde_fase2({}).values())
    assert all(v is None for v in _candidatos_desde_fase2(None).values())


# ── Tests de integración: resolvedores con candidatos_fase2 ─────────────────

def test_resolver_nif_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """Con entities vacías y fase2 presente → candidato fase2_ocr en resultado."""
    candidatos_f2 = _candidatos_desde_fase2({
        "identificacion": {"nif_entidad": {"valor": "B15026693", "confianza": 0.95}}
    })
    res = resolver_nif_entidad(extractor_vacio, candidatos_fase2=candidatos_f2)

    fuentes = [c["fuente"] for c in res.candidatos]
    assert "fase2_ocr" in fuentes
    assert res.decision != DecisionCampo.BLOCK  # Al menos WARN


def test_resolver_nombre_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """nombre_entidad siempre era block sin DocAI → con fase2 debe ser al menos WARN."""
    candidatos_f2 = _candidatos_desde_fase2({
        "identificacion": {"nombre_entidad": {"valor": "ESPINA&DELFIN", "confianza": 0.95}}
    })
    res = resolver_nombre_entidad(extractor_vacio, candidatos_fase2=candidatos_f2)

    assert res.decision != DecisionCampo.BLOCK
    assert any(c["fuente"] == "fase2_ocr" for c in res.candidatos)


def test_resolver_numero_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """numero_factura con fase2 → candidato presente, no elige basura."""
    candidatos_f2 = _candidatos_desde_fase2({
        "identificacion": {"numero_factura": {"valor": "N27253401514", "confianza": 0.95}}
    })
    res = resolver_numero_factura(extractor_vacio, candidatos_fase2=candidatos_f2)

    assert res.decision != DecisionCampo.BLOCK
    assert res.valor_final == "N27253401514"


def test_resolver_fecha_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """fecha_expedicion con fase2 → conf 0.75 (mayor que regex 0.68)."""
    candidatos_f2 = _candidatos_desde_fase2({
        "identificacion": {"fecha_expedicion": {"valor": "2026-02-11", "confianza": 0.95}}
    })
    res = resolver_fecha_expedicion(extractor_vacio, candidatos_fase2=candidatos_f2)

    assert res.confianza_final == pytest.approx(0.75)
    assert res.valor_final == "2026-02-11"


def test_resolver_nif_no_duplica_si_regex_encuentra_mismo(extractor_con_texto):
    """Si regex y fase2 encuentran el mismo NIF → solo 1 candidato con ese valor."""
    candidatos_f2 = _candidatos_desde_fase2({
        "identificacion": {"nif_entidad": {"valor": "B15026693", "confianza": 0.95}}
    })
    res = resolver_nif_entidad(extractor_con_texto, candidatos_fase2=candidatos_f2)

    # Solo debe haber 1 candidato con valor "B15026693"
    b15 = [c for c in res.candidatos if c["valor_normalizado"] == "B15026693"]
    assert len(b15) == 1


def test_resolver_sin_fase2_backward_compatible(extractor_vacio):
    """Sin candidatos_fase2 → comportamiento idéntico al anterior (sin romper)."""
    res = resolver_nif_entidad(extractor_vacio)
    assert res.decision == DecisionCampo.BLOCK  # Sin fuentes → block
    assert len(res.candidatos) == 0


# ── Tests de candidatos_desde_fase2 para receptor ─────────────────────────────

def test_candidatos_desde_fase2_nif_receptor_valido():
    """NIF receptor con checksum OK → candidato con conf=0.75."""
    doc = {"cliente_destino": {"nif_receptor": {"valor": "B15026693", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nif_receptor"]
    assert c is not None
    assert c.fuente == FuenteCandidato.FASE2_OCR
    assert c.confianza == pytest.approx(0.75)
    assert c.validacion_ok is True
    assert c.valor_normalizado == "B15026693"


def test_candidatos_desde_fase2_nombre_receptor():
    """Nombre receptor → conf=0.75, normalizado a mayúsculas."""
    doc = {"cliente_destino": {"nombre_receptor": {"valor": "Restaurante Pepe S.L.", "confianza": 0.95}}}
    resultado = _candidatos_desde_fase2(doc)

    c = resultado["nombre_receptor"]
    assert c is not None
    assert c.fuente == FuenteCandidato.FASE2_OCR
    assert c.confianza == pytest.approx(0.75)
    assert c.valor_normalizado == "RESTAURANTE PEPE S.L."


def test_candidatos_desde_fase2_receptor_nulos():
    """Campos receptor con valor null → None."""
    doc = {"cliente_destino": {
        "nif_receptor": {"valor": None},
        "nombre_receptor": {"valor": None},
    }}
    resultado = _candidatos_desde_fase2(doc)
    assert resultado["nif_receptor"] is None
    assert resultado["nombre_receptor"] is None


def test_candidatos_desde_fase2_sin_cliente_destino():
    """Sin sección cliente_destino → receptor None."""
    doc = {"identificacion": {"nif_entidad": {"valor": "B15026693"}}}
    resultado = _candidatos_desde_fase2(doc)
    assert resultado["nif_receptor"] is None
    assert resultado["nombre_receptor"] is None


# ── Tests de integración: resolvedores receptor con candidatos_fase2 ──────────

def test_resolver_nif_receptor_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """Con entities vacías y fase2 presente → candidato fase2_ocr en resultado."""
    candidatos_f2 = _candidatos_desde_fase2({
        "cliente_destino": {"nif_receptor": {"valor": "B15026693", "confianza": 0.95}}
    })
    res = resolver_nif_receptor(extractor_vacio, candidatos_fase2=candidatos_f2)

    fuentes = [c["fuente"] for c in res.candidatos]
    assert "fase2_ocr" in fuentes
    assert res.decision != DecisionCampo.BLOCK


def test_resolver_nombre_receptor_usa_fase2_cuando_entities_vacias(extractor_vacio):
    """nombre_receptor con fase2 → al menos WARN."""
    candidatos_f2 = _candidatos_desde_fase2({
        "cliente_destino": {"nombre_receptor": {"valor": "RESTAURANTE PEPE SL", "confianza": 0.95}}
    })
    res = resolver_nombre_receptor(extractor_vacio, candidatos_fase2=candidatos_f2)

    assert res.decision != DecisionCampo.BLOCK
    assert any(c["fuente"] == "fase2_ocr" for c in res.candidatos)


def test_resolver_nif_receptor_sin_fuentes_es_block(extractor_vacio):
    """Sin ninguna fuente → BLOCK."""
    res = resolver_nif_receptor(extractor_vacio)
    assert res.decision == DecisionCampo.BLOCK
    assert len(res.candidatos) == 0


def test_resolver_nif_receptor_con_receiver_tax_id():
    """Document AI receiver_tax_id → candidato nativo con checksum."""
    extractor = DocumentAIEntityExtractor({
        "entities": [{
            "type_": "receiver_tax_id",
            "mentionText": "B15026693",
            "confidence": 0.92,
            "pageAnchor": {"pageRefs": [{"page": 0}]},
            "textAnchor": {"textSegments": [{"startIndex": 100, "endIndex": 109}]},
        }],
        "text": "x" * 200,
        "pages": [],
    })
    res = resolver_nif_receptor(extractor)

    assert res.valor_final == "B15026693"
    assert res.decision == DecisionCampo.AUTO
    assert len(res.candidatos) >= 1
    assert res.candidatos[0]["fuente"] == "document_ai_nativo"


def test_resolver_nombre_receptor_con_receiver_name():
    """Document AI receiver_name → candidato nativo."""
    extractor = DocumentAIEntityExtractor({
        "entities": [{
            "type_": "receiver_name",
            "mentionText": "Restaurante Pepe S.L.",
            "confidence": 0.90,
            "pageAnchor": {"pageRefs": [{"page": 0}]},
            "textAnchor": {"textSegments": [{"startIndex": 50, "endIndex": 72}]},
        }],
        "text": "x" * 200,
        "pages": [],
    })
    res = resolver_nombre_receptor(extractor)

    assert res.valor_final == "RESTAURANTE PEPE S.L."
    assert res.decision == DecisionCampo.AUTO
    assert len(res.candidatos) >= 1
