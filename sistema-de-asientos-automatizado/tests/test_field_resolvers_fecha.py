"""
Tests unitarios para resolución de fechas con múltiples entity_types.

Verifica que resolver_fecha_expedicion() recolecta candidatos de todos
los tipos de fecha disponibles (invoice_date, delivery_date, receive_date, due_date)
para permitir que el LLM árbitro elija la correcta.
"""

import json
from pathlib import Path

import pytest

from src.phase3_identidad_cabecera.docai_extractor import DocumentAIEntityExtractor
from src.phase3_identidad_cabecera.field_resolvers import resolver_fecha_expedicion
from src.phase3_identidad_cabecera.field_candidate import DecisionCampo


def test_resolver_fecha_expedicion_multiples_tipos():
    """
    Verifica que resolver_fecha_expedicion recolecta candidatos de todos los
    tipos de fecha cuando invoice_date no está presente.

    Caso: factura sin invoice_date pero con delivery_date.
    Resultado esperado: delivery_date se recolecta como candidato viable.
    """
    # Mock de raw_document_ai.json con delivery_date pero sin invoice_date
    raw_doc = {
        "text": "Factura de prueba\nFecha de entrega: 15/03/2026",
        "entities": [
            {
                "type": "delivery_date",
                "mentionText": "15/03/2026",
                "normalizedValue": {"text": "2026-03-15"},
                "confidence": 0.92,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            }
        ],
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1.0, "height": 1.0, "unit": "pixel"}
            }
        ],
    }

    extractor = DocumentAIEntityExtractor(raw_doc)
    resolution = resolver_fecha_expedicion(extractor)

    # Debe tener al menos 1 candidato (delivery_date)
    assert len(resolution.candidatos) >= 1, "Debe recolectar delivery_date como candidato"

    # El resolver usa descripciones legibles en español, no el entity_type literal.
    # delivery_date → "Fecha de entrega (fallback, confianza reducida)"
    candidato_delivery = None
    for c in resolution.candidatos:
        if "Fecha de entrega" in c.get("motivo", ""):
            candidato_delivery = c
            break

    assert candidato_delivery is not None, "Debe existir candidato de delivery_date (Fecha de entrega)"
    assert candidato_delivery["valor_normalizado"] == "2026-03-15"
    assert candidato_delivery["validacion_ok"] is True


def test_resolver_fecha_expedicion_sin_duplicados():
    """
    Verifica que no se crean candidatos duplicados cuando múltiples
    entity_types tienen el mismo valor normalizado.

    Caso: invoice_date y delivery_date ambos con la misma fecha.
    Resultado esperado: solo 1 candidato con ese valor.
    """
    raw_doc = {
        "text": "Factura de prueba\nFecha: 20/03/2026",
        "entities": [
            {
                "type": "invoice_date",
                "mentionText": "20/03/2026",
                "normalizedValue": {"text": "2026-03-20"},
                "confidence": 0.95,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
            {
                "type": "delivery_date",
                "mentionText": "20/03/2026",
                "normalizedValue": {"text": "2026-03-20"},
                "confidence": 0.88,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
        ],
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1.0, "height": 1.0, "unit": "pixel"}
            }
        ],
    }

    extractor = DocumentAIEntityExtractor(raw_doc)
    resolution = resolver_fecha_expedicion(extractor)

    # Contar cuántos candidatos tienen el valor "2026-03-20"
    candidatos_con_valor = [
        c for c in resolution.candidatos
        if c.get("valor_normalizado") == "2026-03-20"
    ]

    assert len(candidatos_con_valor) == 1, (
        "No debe crear candidatos duplicados para el mismo valor normalizado"
    )

    # El candidato elegido debe ser el de mayor confianza (invoice_date → "Fecha de factura")
    assert "Fecha de factura" in candidatos_con_valor[0].get("motivo", "")


def test_resolver_fecha_expedicion_multiples_fechas_distintas():
    """
    Verifica que cuando hay múltiples fechas diferentes, se recolectan todas
    como candidatos separados para que el LLM pueda arbitrar.

    Caso: invoice_date, delivery_date y due_date con valores distintos.
    Resultado esperado: 3 candidatos distintos.
    """
    raw_doc = {
        "text": "Factura: 10/03/2026, Entrega: 15/03/2026, Vencimiento: 20/03/2026",
        "entities": [
            {
                "type": "invoice_date",
                "mentionText": "10/03/2026",
                "normalizedValue": {"text": "2026-03-10"},
                "confidence": 0.85,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
            {
                "type": "delivery_date",
                "mentionText": "15/03/2026",
                "normalizedValue": {"text": "2026-03-15"},
                "confidence": 0.90,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
            {
                "type": "due_date",
                "mentionText": "20/03/2026",
                "normalizedValue": {"text": "2026-03-20"},
                "confidence": 0.88,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
        ],
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1.0, "height": 1.0, "unit": "pixel"}
            }
        ],
    }

    extractor = DocumentAIEntityExtractor(raw_doc)
    resolution = resolver_fecha_expedicion(extractor)

    # Debe tener 3 candidatos con valores distintos
    valores_unicos = set(
        c.get("valor_normalizado")
        for c in resolution.candidatos
        if c.get("valor_normalizado")
    )

    assert len(valores_unicos) >= 3, (
        f"Debe recolectar las 3 fechas distintas como candidatos. "
        f"Encontrados: {valores_unicos}"
    )

    # El resolver usa descripciones legibles en español en el campo motivo.
    # invoice_date → "Fecha de factura", delivery_date → "Fecha de entrega",
    # due_date → "Fecha de vencimiento"
    motivos = " ".join(c.get("motivo", "") for c in resolution.candidatos)
    assert "Fecha de factura" in motivos
    assert "Fecha de entrega" in motivos
    assert "Fecha de vencimiento" in motivos


def test_resolver_fecha_expedicion_solo_invoice_date():
    """
    Verifica que cuando solo hay invoice_date, el comportamiento es normal
    (backward compatibility).

    Caso: solo invoice_date con alta confianza.
    Resultado esperado: AUTO, 1 candidato.
    """
    raw_doc = {
        "text": "Factura del 25/03/2026",
        "entities": [
            {
                "type": "invoice_date",
                "mentionText": "25/03/2026",
                "normalizedValue": {"text": "2026-03-25"},
                "confidence": 0.95,
                "pageAnchor": {"pageRefs": [{"page": 0}]},
            },
        ],
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1.0, "height": 1.0, "unit": "pixel"}
            }
        ],
    }

    extractor = DocumentAIEntityExtractor(raw_doc)
    resolution = resolver_fecha_expedicion(extractor)

    assert resolution.decision == DecisionCampo.AUTO
    assert resolution.valor_final == "2026-03-25"
    assert len(resolution.candidatos) >= 1
    # El resolver usa descripciones en español: invoice_date → "Fecha de factura"
    assert "Fecha de factura" in resolution.candidatos[0].get("motivo", "")


def test_resolver_fecha_expedicion_sin_ninguna_fecha():
    """
    Verifica el comportamiento cuando no hay ninguna entidad de fecha.

    Caso: documento sin fechas de Document AI.
    Resultado esperado: BLOCK o regex contextual como fallback.
    """
    raw_doc = {
        "text": "Factura sin fecha detectada por Document AI",
        "entities": [],
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1.0, "height": 1.0, "unit": "pixel"}
            }
        ],
    }

    extractor = DocumentAIEntityExtractor(raw_doc)
    resolution = resolver_fecha_expedicion(extractor)

    # Si no hay fechas ni en Document AI ni en regex, debe ser BLOCK
    # (o tener candidatos de regex contextual si el texto tiene "Fecha:" seguido de patrón)
    if not resolution.candidatos or resolution.valor_final is None:
        assert resolution.decision == DecisionCampo.BLOCK
