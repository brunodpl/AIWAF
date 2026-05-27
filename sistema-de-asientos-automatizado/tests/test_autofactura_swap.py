"""
Tests para la detección de autofactura ("facturación por el destinatario")
e inversión emisor/receptor en la fase 3 de identidad.

Hallazgo F1 (auditoría E2E INDUSTRIAL): en autofacturas la operadora aparece
impresa como emisor, así que el pipeline resuelve nif_entidad = operadora y
nif_receptor = titular del local. Como en ventas el cliente = nif_entidad, el
cliente resuelve a la operadora (incorrecto). El emisor legal es el titular.

CabeceraResolver debe, cuando detecta el marcador de autofactura en el OCR e
identifica ambos NIF válidos, intercambiar nif_entidad<->nif_receptor y
nombre_entidad<->nombre_receptor, forzando decisión >= WARN (revisión humana).
"""

from src.config import Settings
from src.phase3_identidad_cabecera.cabecera_resolver import (
    CabeceraResolver,
    _es_autofactura,
)
from src.phase3_identidad_cabecera.field_candidate import DecisionCampo


_MARCADORES = ["FACTURACION POR EL DESTINATARIO"]

# COMAR (operadora) impresa como emisor; Paula (titular) como receptor.
_COMAR_NIF = "B15614480"
_COMAR_NOMBRE = "COMAR CORUÑA S.L."
_PAULA_NIF = "46896438J"
_PAULA_NOMBRE = "SUEIRO PARDO, PAULA"


def _raw_autofactura(con_marcador: bool) -> dict:
    """Raw Document AI de una autofactura: COMAR como supplier, Paula como receiver."""
    texto = "Participación local 111,90\nTOTAL RECIBIDO 135,40\n"
    if con_marcador:
        texto += "FACTURACIÓN POR EL DESTINATARIO\n"
    return {
        "text": texto,
        "entities": [
            {"type": "supplier_tax_id", "mentionText": _COMAR_NIF, "confidence": 0.97},
            {"type": "supplier_name", "mentionText": _COMAR_NOMBRE, "confidence": 0.96},
            {"type": "receiver_tax_id", "mentionText": _PAULA_NIF, "confidence": 0.97},
            {"type": "receiver_name", "mentionText": _PAULA_NOMBRE, "confidence": 0.96},
            {"type": "invoice_id", "mentionText": "M/24/1", "confidence": 0.94},
            {"type": "invoice_date", "mentionText": "31/01/2024",
             "confidence": 0.93, "normalizedValue": {"text": "2024-01-31"}},
        ],
    }


def _resolver() -> CabeceraResolver:
    return CabeceraResolver(
        usar_llm=False,
        umbral_auto=0.90,
        umbral_warn=0.65,
        autofactura_marcadores=_MARCADORES,
    )


class TestAutofacturaSwap:

    def test_invierte_emisor_y_receptor_con_marcador(self):
        resultado = _resolver().resolver(_raw_autofactura(con_marcador=True), "doc-auto")

        # Emisor legal pasa a ser el titular (Paula); contraparte = operadora (COMAR).
        assert resultado.nif_entidad["valor_final"] == _PAULA_NIF
        assert resultado.nif_receptor["valor_final"] == _COMAR_NIF
        assert resultado.nombre_entidad["valor_final"] == _PAULA_NOMBRE.upper()
        assert resultado.nombre_receptor["valor_final"] == _COMAR_NOMBRE.upper()

    def test_swap_fuerza_revision_humana_con_motivo(self):
        resultado = _resolver().resolver(_raw_autofactura(con_marcador=True), "doc-auto")

        for campo in (resultado.nif_entidad, resultado.nif_receptor,
                      resultado.nombre_entidad, resultado.nombre_receptor):
            assert campo["decision"] in (DecisionCampo.WARN.value, DecisionCampo.BLOCK.value)
            assert "AUTOFACTURA" in campo["motivo"].upper()

    def test_sin_marcador_no_hay_swap(self):
        resultado = _resolver().resolver(_raw_autofactura(con_marcador=False), "doc-normal")

        # Sin marcador, comportamiento idéntico al actual: COMAR sigue como emisor.
        assert resultado.nif_entidad["valor_final"] == _COMAR_NIF
        assert resultado.nif_receptor["valor_final"] == _PAULA_NIF
        assert resultado.nif_entidad["decision"] == DecisionCampo.AUTO.value

    def test_marcador_pero_sin_nif_receptor_no_invierte(self):
        # Gating: si falta uno de los NIF, no se puede invertir con seguridad.
        raw = _raw_autofactura(con_marcador=True)
        raw["entities"] = [e for e in raw["entities"] if e["type"] != "receiver_tax_id"]
        resultado = _resolver().resolver(raw, "doc-parcial")

        assert resultado.nif_entidad["valor_final"] == _COMAR_NIF


class TestMarcadoresDefaultAmpliados:
    """El default de config debe cubrir variantes habituales de autofactura.

    Muchas autofacturas (máquinas recreativas) no usan literalmente
    "FACTURACIÓN POR EL DESTINATARIO". El default debe reconocer variantes
    para que el swap salte sin necesidad de tocar el .env por gestoría.
    """

    def _marcadores_default(self) -> list[str]:
        raw = Settings.model_fields["autofactura_marcadores_raw"].default
        return [m.strip() for m in raw.split("|") if m.strip()]

    def test_default_reconoce_variantes(self):
        marcadores = self._marcadores_default()
        textos = [
            "Total 100\nFACTURACIÓN POR EL DESTINATARIO\n",
            "Esta es una FACTURA EXPEDIDA POR EL DESTINATARIO de la operación",
            "FACTURA EMITIDA POR EL DESTINATARIO en nombre del titular",
            "Documento tipo AUTOFACTURA",
        ]
        for texto in textos:
            assert _es_autofactura(texto, marcadores), f"no reconocido: {texto!r}"

    def test_default_no_marca_factura_normal(self):
        marcadores = self._marcadores_default()
        assert not _es_autofactura(
            "Factura ordinaria\nBase 100\nIVA 21\nTOTAL 121\n", marcadores
        )
