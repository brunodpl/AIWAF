"""
Extractor de entidades nativas de Document AI.

Lee el raw JSON de Document AI y extrae entidades con:
  - mention_text (texto raw)
  - normalized_value (valor normalizado si existe)
  - confidence
  - textAnchor (offsets de posición en el texto)
  - pageAnchor → pageRefs → boundingPoly (coordenadas normalizadas + número de página)

El normalized_value se prefiere sobre mention_text para:
  - Fechas: Document AI devuelve ISO 8601 en normalized_value.text
  - Numéricos: normalized_value.money_value.amount o .text con formato estable
  - NIFs: mention_text suele ser más fiel (Document AI no normaliza identificadores)

Nomenclatura de entity_type del Invoice Parser según documentación oficial:
  supplier_name, supplier_tax_id, supplier_address
  receiver_name, receiver_tax_id
  invoice_id, invoice_date, due_date, invoice_type
  total_amount, net_amount
  vat (con sub-entidades vat/amount, vat/tax_rate, vat/tax_amount)
  line_item (con sub-entidades line_item/description, line_item/amount)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RawEntity:
    """
    Entidad extraída del raw JSON de Document AI.

    Contiene tanto el texto raw como el valor normalizado si existe,
    más trazabilidad de ubicación completa.
    """
    entity_type: str
    mention_text: Optional[str]
    normalized_text: Optional[str]    # De normalized_value.text (fechas, algunos numéricos)
    normalized_float: Optional[float] # De normalized_value.money_value.amount (importes)
    confidence: float
    page_number: int                  # Número de página (0-indexed, default 0)
    bbox_normalizado: Optional[dict]  # {x_min,y_min,x_max,y_max} en [0,1] o None
    text_anchor_offsets: Optional[list]  # [{startIndex, endIndex}]

    @property
    def valor_preferido(self) -> Optional[str]:
        """
        Valor preferido: normalized_text si existe, sino mention_text.

        Para identificadores fiscales (supplier_tax_id) usamos mention_text
        porque Document AI no normaliza NIFs de forma fiable.
        Para fechas e importes usamos normalized_text.
        """
        return self.normalized_text or self.mention_text

    @property
    def valor_para_nif(self) -> Optional[str]:
        """
        Para NIF/CIF: preferir mention_text sobre normalized_value.
        Document AI puede alterar el formato al normalizar identificadores.
        """
        return self.mention_text or self.normalized_text


def _extraer_bbox_desde_page_refs(page_refs: list) -> tuple[Optional[dict], int]:
    """
    Extraer bbox normalizado y número de página desde pageRefs de Document AI.

    Document AI estructura el ancla visual así (raw JSON, camelCase):
      pageAnchor.pageRefs[].page            ← índice de página (ausente = 0)
      pageAnchor.pageRefs[].boundingPoly    ← coordenadas

    Solo usamos normalizedVertices. Los vertices absolutos dependen del DPI
    interno del procesador y no son seguros sin conocer ese DPI.

    Returns:
        Tupla (bbox_dict o None, numero_pagina)
    """
    if not page_refs:
        return None, 0

    for pr in page_refs:
        if not isinstance(pr, dict):
            continue

        pagina = int(pr.get("page", 0))
        bp = pr.get("boundingPoly") or pr.get("bounding_poly", {})
        if not bp:
            continue

        # Solo normalizedVertices (camelCase en API REST, snake_case en proto-dict)
        nverts = bp.get("normalizedVertices") or bp.get("normalized_vertices", [])
        if not nverts or len(nverts) < 2:
            continue

        xs = [float(v.get("x", 0)) for v in nverts if isinstance(v, dict)]
        ys = [float(v.get("y", 0)) for v in nverts if isinstance(v, dict)]

        if not xs or not ys or (max(xs) == 0 and max(ys) == 0):
            continue

        return {
            "x_min": min(xs), "y_min": min(ys),
            "x_max": max(xs), "y_max": max(ys),
        }, pagina

    return None, 0


def _extraer_text_anchor(entity: dict) -> Optional[list]:
    """
    Extraer offsets de textAnchor para trazabilidad de posición en el texto.

    textAnchor.textSegments[].startIndex / endIndex
    """
    ta = entity.get("textAnchor") or entity.get("text_anchor", {})
    if not ta:
        return None

    segments = ta.get("textSegments") or ta.get("text_segments", [])
    if not segments:
        return None

    return [
        {
            "startIndex": int(s.get("startIndex", s.get("start_index", 0))),
            "endIndex": int(s.get("endIndex", s.get("end_index", 0))),
        }
        for s in segments
        if isinstance(s, dict)
    ]


def _extraer_normalized_value(entity: dict) -> tuple[Optional[str], Optional[float]]:
    """
    Extraer valor normalizado de un entity de Document AI.

    Document AI puede devolver:
    - normalized_value.text: string normalizado (fechas en ISO 8601, etc.)
    - normalized_value.money_value.amount: float para importes
    - normalized_value.date_value: dict {year, month, day}

    Returns:
        Tupla (texto_normalizado, valor_float)
    """
    nv = entity.get("normalizedValue") or entity.get("normalized_value", {})
    if not nv:
        return None, None

    # Texto normalizado (fechas, strings)
    texto = nv.get("text")

    # Importe monetario
    mv = nv.get("moneyValue") or nv.get("money_value", {})
    valor_float = None
    if mv:
        amount = mv.get("amount") or mv.get("units")
        if amount is not None:
            try:
                valor_float = float(amount)
                # Document AI devuelve amount en unidades enteras más nanos
                nanos = float(mv.get("nanos", 0))
                if nanos:
                    valor_float += nanos / 1e9
            except (TypeError, ValueError):
                pass

    # date_value como alternativa a texto ISO
    if not texto:
        dv = nv.get("dateValue") or nv.get("date_value", {})
        if dv:
            try:
                y = dv.get("year", 0)
                m = dv.get("month", 0)
                d = dv.get("day", 0)
                if y and m and d:
                    texto = f"{y:04d}-{m:02d}-{d:02d}"
            except (TypeError, ValueError):
                pass

    return texto or None, valor_float


class DocumentAIEntityExtractor:
    """
    Extrae entidades del raw JSON de Document AI con trazabilidad completa.

    Acepta el formato de la API REST (camelCase) y el formato proto-dict (snake_case).
    Extrae mention_text, normalized_value, confidence, textAnchor y pageAnchor.
    """

    def __init__(self, raw_document_ai: dict):
        """
        Args:
            raw_document_ai: Dict del response de Document AI.
                Puede ser el response completo (con key 'document')
                o directamente el document dict.
        """
        # El response puede venir envuelto en {'document': {...}} o directamente
        if "document" in raw_document_ai and isinstance(raw_document_ai["document"], dict):
            self._doc = raw_document_ai["document"]
        else:
            self._doc = raw_document_ai

        self._entities = self._doc.get("entities", [])

    def extraer_por_tipo(self, entity_type: str) -> list[RawEntity]:
        """
        Extraer todas las entidades de un tipo dado.

        Para la mayoría de campos del Invoice Parser solo hay 1 entidad del tipo
        (supplier_name, invoice_id...). Para vat y line_item puede haber varias.

        Args:
            entity_type: Tipo exacto del entity (p.e. "supplier_tax_id")

        Returns:
            Lista de RawEntity en orden de aparición en el documento
        """
        resultado = []

        for entity in self._entities:
            tipo = (
                entity.get("type_")
                or entity.get("type")
                or entity.get("entityType", "")
            )
            if tipo != entity_type:
                continue

            mention = entity.get("mentionText") or entity.get("mention_text")
            confidence = float(entity.get("confidence", 0.0))

            norm_texto, norm_float = _extraer_normalized_value(entity)
            text_anchor = _extraer_text_anchor(entity)

            page_anchor = (
                entity.get("pageAnchor")
                or entity.get("page_anchor", {})
            )
            page_refs = (
                page_anchor.get("pageRefs")
                or page_anchor.get("page_refs", [])
            ) if page_anchor else []

            bbox, pagina = _extraer_bbox_desde_page_refs(page_refs)

            resultado.append(RawEntity(
                entity_type=entity_type,
                mention_text=mention,
                normalized_text=norm_texto,
                normalized_float=norm_float,
                confidence=confidence,
                page_number=pagina,
                bbox_normalizado=bbox,
                text_anchor_offsets=text_anchor,
            ))

        return resultado

    def extraer_primero(self, entity_type: str) -> Optional[RawEntity]:
        """
        Extraer la primera entidad de un tipo dado.
        El 99% de los campos del Invoice Parser tienen como máximo una entidad.
        """
        entities = self.extraer_por_tipo(entity_type)
        return entities[0] if entities else None

    def texto_completo(self) -> str:
        """Texto OCR completo del documento para búsquedas contextuales."""
        return self._doc.get("text", "")
