"""
E2E determinista — Fase 3 identidad ⇒ Fase 4 cliente (autofactura).

Cubre el hueco que los unitarios no pueden cubrir: que la inversión emisor/
receptor detectada en la fase 3 (cabecera_resolver) **propague correctamente**
a la fase 4 (resolver_cliente) y resuelva al titular del local como cliente,
no a la operadora.

Hallazgo F1 de la auditoría e2e industrial (2026-05-22): en autofacturas de
máquinas recreativas, la operadora (COMAR) se imprime como emisor y el titular
(Paula) como receptor. Sin swap, el cliente resuelve a COMAR; con swap
correcto, resuelve a Paula.

Este test corre la lógica REAL de ambas fases — solo el OCR/LLM externo
quedaría mockeado, pero como `usar_llm=False` y los entities de DocAI vienen
inline, no hace falta ningún patch.
"""

from __future__ import annotations

import pytest

from src.phase3_identidad_cabecera.cabecera_resolver import CabeceraResolver
from src.phase4_customer.resolver import resolver_cliente


# Datos verosímiles tomados del caso real documentado en la auditoría
# (lote "FACTURAS INDUSTRIAL 2024 2025 2026.pdf", 60/60 facturas).
_MARCADORES_AUTOFACTURA = ["FACTURACION POR EL DESTINATARIO"]

_COMAR_NIF = "B15614480"
_COMAR_NOMBRE = "COMAR CORUÑA S.L."
_PAULA_NIF = "46896438J"
_PAULA_NOMBRE = "SUEIRO PARDO, PAULA"


def _raw_docai_autofactura() -> dict:
    """Raw del Document AI Invoice Parser para una autofactura típica.

    Mismos entities que `test_autofactura_swap.py` (la fuente de verdad del
    contrato de identidad) para que cualquier divergencia se vea como bug
    de contrato, no como deriva del test e2e.
    """
    texto = (
        "Participación local 111,90\n"
        "TOTAL RECIBIDO 135,40\n"
        "FACTURACIÓN POR EL DESTINATARIO\n"
    )
    return {
        "text": texto,
        "entities": [
            {"type": "supplier_tax_id", "mentionText": _COMAR_NIF, "confidence": 0.97},
            {"type": "supplier_name", "mentionText": _COMAR_NOMBRE, "confidence": 0.96},
            {"type": "receiver_tax_id", "mentionText": _PAULA_NIF, "confidence": 0.97},
            {"type": "receiver_name", "mentionText": _PAULA_NOMBRE, "confidence": 0.96},
            {"type": "invoice_id", "mentionText": "M/24/1", "confidence": 0.94},
            {
                "type": "invoice_date",
                "mentionText": "31/01/2024",
                "confidence": 0.93,
                "normalizedValue": {"text": "2024-01-31"},
            },
        ],
    }


def _resolver_cabecera() -> CabeceraResolver:
    return CabeceraResolver(
        usar_llm=False,
        umbral_auto=0.90,
        umbral_warn=0.65,
        autofactura_marcadores=_MARCADORES_AUTOFACTURA,
        autofactura_swap_enabled=True,
    )


def _campos_identidad_from(result) -> dict:
    """Convierte CabeceraResult al formato que espera resolver_cliente.

    resolver_cliente lee `campos_identidad[<campo>]["valor_final" | "confianza_final" | "decision"]`
    y CabeceraResult ya almacena cada campo como ese mismo dict (vía FieldResolution.to_dict()).
    """
    return {
        "nif_entidad": result.nif_entidad,
        "nombre_entidad": result.nombre_entidad,
        "nif_receptor": result.nif_receptor,
        "nombre_receptor": result.nombre_receptor,
    }


def test_autofactura_cliente_resuelve_al_titular_del_local():
    """Integración fase3 ⇒ fase4: en una autofactura de ventas, el cliente
    final resuelto debe ser el TITULAR del local (Paula, receptor en el OCR),
    no la operadora (COMAR, supplier en el OCR).

    Este es el comportamiento contractual entre fases: aunque la fase 3
    pueda invertir los roles internamente, la fase 4 debe leerlos ya
    invertidos. Si el swap se rompe en cualquier punto del camino, este
    test lo caza.
    """
    resultado_identidad = _resolver_cabecera().resolver(
        _raw_docai_autofactura(), "doc-autofactura-industrial-1"
    )

    cliente = resolver_cliente(
        campos_identidad=_campos_identidad_from(resultado_identidad),
        libro="21_VENTAS_INGRESOS",
        maestro={"clientes": {}},
        documento_id="doc-autofactura-industrial-1",
    )

    nif_cliente = cliente["campos"]["nif_cliente"]["valor_final"]
    nombre_cliente = cliente["campos"]["nombre_cliente"]["valor_final"]

    assert nif_cliente == _PAULA_NIF, (
        f"En una autofactura, el cliente debe ser el titular del local "
        f"({_PAULA_NIF} = {_PAULA_NOMBRE}), no la operadora "
        f"({_COMAR_NIF} = {_COMAR_NOMBRE}). Resuelto: {nif_cliente} / {nombre_cliente}."
    )
    assert nombre_cliente and _PAULA_NOMBRE.split(",")[0].upper() in nombre_cliente.upper()


def test_autofactura_nunca_autocarga():
    """Aunque el swap funcione, una autofactura SIEMPRE debe forzar revisión
    humana (decision_global != auto). Es un caso fiscal sensible y un cliente
    desconocido en maestro debe siempre escalarse.
    """
    resultado_identidad = _resolver_cabecera().resolver(
        _raw_docai_autofactura(), "doc-autofactura-industrial-1"
    )

    cliente = resolver_cliente(
        campos_identidad=_campos_identidad_from(resultado_identidad),
        libro="21_VENTAS_INGRESOS",
        maestro={"clientes": {}},
        documento_id="doc-autofactura-industrial-1",
    )

    assert cliente["decision_global"] in ("warn", "block"), (
        f"Una autofactura nunca debe autocargarse en silencio. "
        f"decision_global={cliente['decision_global']}"
    )


def test_ventas_normal_cliente_resuelve_al_emisor():
    """Caso base / smoke: en una factura de ventas normal (sin marcador de
    autofactura), el cliente resuelve al EMISOR (`nif_entidad`), tal como
    define el mapeo LIBRO_A_ROL_CLIENTE para 21_VENTAS_INGRESOS.

    Sirve como red de seguridad para que un cambio en el swap no rompa
    el camino feliz.
    """
    # Mismos entities que la autofactura pero sin el marcador en el texto:
    # no debe activarse el swap.
    raw = _raw_docai_autofactura()
    raw["text"] = "Factura ordinaria\nBase 100\nIVA 21\nTOTAL 121\n"

    resultado_identidad = _resolver_cabecera().resolver(raw, "doc-ventas-normal")

    cliente = resolver_cliente(
        campos_identidad=_campos_identidad_from(resultado_identidad),
        libro="21_VENTAS_INGRESOS",
        maestro={
            "clientes": {
                _COMAR_NIF: {"nombre": _COMAR_NOMBRE, "primer_uso": "2024-01-01"},
            }
        },
        documento_id="doc-ventas-normal",
    )

    # Sin marcador, el emisor (COMAR) sigue siendo el cliente; está en maestro
    # → puede llegar a auto (sin penalización por cliente nuevo).
    assert cliente["campos"]["nif_cliente"]["valor_final"] == _COMAR_NIF
    assert cliente["cliente_info"]["es_nuevo"] is False
