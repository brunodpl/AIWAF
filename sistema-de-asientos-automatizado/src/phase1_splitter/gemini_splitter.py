"""Cliente Gemini Vision para detectar cortes en PDFs multi-factura.

Envía el PDF binario directo a Gemini (sin OCR previo) con un prompt que
le pide los rangos de páginas que componen cada factura individual.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from google.genai import types

logger = logging.getLogger("pipeline.splitter")


_PROMPT = """Eres un sistema de pre-procesado de facturas. Recibes un PDF que puede contener
una o varias facturas distintas escaneadas en el mismo fichero (lote ADF).

Tu tarea: identificar cuántas facturas independientes contiene el PDF y qué
rango de páginas (1-based) corresponde a cada una.

Dos páginas pertenecen a la MISMA factura si comparten emisor, número de
factura y fecha. Cambia de factura cuando aparece un nuevo encabezado con
otro emisor, otro número de factura o un total/cierre seguido de un nuevo
encabezado.

Devuelve EXCLUSIVAMENTE un JSON con esta forma, sin texto adicional:
{
  "facturas": [
    {"paginas": [1], "emisor_cif_aparente": "B12345678", "confidence": 0.95},
    {"paginas": [2, 3], "emisor_cif_aparente": "A87654321", "confidence": 0.90}
  ]
}

Reglas:
- "paginas" es una lista 1-based de enteros consecutivos.
- Cada página del PDF aparece en EXACTAMENTE una factura.
- Las listas "paginas" en conjunto cubren todas las páginas del PDF, sin
  solapamientos ni huecos.
- "emisor_cif_aparente" puede ser null si no se identifica con claridad.
- "confidence" es un float 0..1 con tu confianza en el corte.
- Si el PDF contiene una sola factura, devuelve un array con un único
  elemento que cubra todas las páginas.
"""


@dataclass(frozen=True)
class SplitGroup:
    """Un grupo de páginas que componen una factura individual."""

    pages: tuple[int, ...]
    emisor_cif_aparente: Optional[str]
    confidence: Optional[float]


@dataclass(frozen=True)
class SplitDecision:
    """Resultado completo del splitter para un PDF."""

    groups: tuple[SplitGroup, ...]
    model_used: str
    latency_ms: int


def detect_splits(
    client,
    pdf_bytes: bytes,
    model: str,
    n_pages_total: int,
) -> SplitDecision:
    """Llama a Gemini Vision sobre ``pdf_bytes`` y devuelve los grupos
    de páginas detectados. Lanza ``ValueError`` si la respuesta no cubre
    exactamente todas las páginas del PDF.
    """
    import time

    start = time.monotonic()
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            _PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    raw_text = _extract_text(response)
    parsed = _parse_json(raw_text)
    groups = _validate_and_build(parsed, n_pages_total)
    return SplitDecision(
        groups=tuple(groups), model_used=model, latency_ms=latency_ms
    )


def _extract_text(response) -> str:
    text = getattr(response, "text", None)
    if text:
        return text
    candidates = getattr(response, "candidates", None) or []
    for c in candidates:
        content = getattr(c, "content", None)
        parts = getattr(content, "parts", None) or []
        for p in parts:
            t = getattr(p, "text", None)
            if t:
                return t
    raise ValueError("Gemini response has no text content")


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_json(raw: str) -> dict:
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini response is not valid JSON: {e}") from e


def _validate_and_build(parsed: dict, n_pages_total: int) -> List[SplitGroup]:
    facturas = parsed.get("facturas")
    if not isinstance(facturas, list) or not facturas:
        raise ValueError("Gemini response missing non-empty 'facturas' array")

    groups: List[SplitGroup] = []
    seen_pages: set[int] = set()

    for idx, item in enumerate(facturas):
        if not isinstance(item, dict):
            raise ValueError(f"facturas[{idx}] is not an object")
        pages = item.get("paginas")
        if not isinstance(pages, list) or not pages:
            raise ValueError(f"facturas[{idx}].paginas must be a non-empty list")
        try:
            pages_int = tuple(int(p) for p in pages)
        except (TypeError, ValueError) as e:
            raise ValueError(f"facturas[{idx}].paginas must be integers: {e}") from e
        for p in pages_int:
            if p < 1 or p > n_pages_total:
                raise ValueError(
                    f"facturas[{idx}] page {p} out of range 1..{n_pages_total}"
                )
            if p in seen_pages:
                raise ValueError(f"page {p} appears in more than one factura")
            seen_pages.add(p)

        confidence = item.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None

        emisor = item.get("emisor_cif_aparente")
        if emisor is not None and not isinstance(emisor, str):
            emisor = None

        groups.append(
            SplitGroup(
                pages=pages_int,
                emisor_cif_aparente=emisor,
                confidence=confidence,
            )
        )

    missing = set(range(1, n_pages_total + 1)) - seen_pages
    if missing:
        raise ValueError(
            f"Gemini split does not cover all pages — missing: {sorted(missing)}"
        )

    return groups
