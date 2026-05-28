"""
Resolvedores por campo de cabecera.

Cada resolvedor implementa la secuencia canónica:
  1. Extraer candidato nativo de Document AI (supplier_tax_id, invoice_id, etc.)
  2. Si normalized_value existe, preferirlo para fechas y numéricos
  3. Aplicar regex contextual sobre el texto OCR completo como fallback
  4. Validar determinísticamente cada candidato
  5. Puntuar y ordenar candidatos
  6. Devolver FieldResolution con todos los candidatos trazados

El LLM no se llama aquí. El CabeceraResolver decide cuándo llamarlo.

Regex contextual: patrones con contexto léxico (palabras clave que preceden
al valor) para reducir falsos positivos en texto libre de facturas.
"""

from __future__ import annotations

import re
import logging
from datetime import date
from typing import Optional

from .docai_extractor import DocumentAIEntityExtractor, RawEntity
from .field_candidate import (
    FieldCandidate, FieldResolution, FuenteCandidato, DecisionCampo
)
from .nif_cif_validator import validar_identificador_fiscal

# Import settings con fallback para tests
try:
    from ..config import settings
except ImportError:
    try:
        from src.config import settings
    except ImportError:
        # Fallback para tests que usan sys.path.insert
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config import settings

logger = logging.getLogger("pipeline.identidad")

# Umbrales de confianza
_UMBRAL_AUTO = 0.90   # >= auto sin LLM
_UMBRAL_WARN = 0.65   # >= warn; < block
_UMBRAL_LLM  = 0.80   # < este umbral → considerar llamar LLM árbitro


# ──────────────────────────────────────────────────────────
# Utilidades de normalización
# ──────────────────────────────────────────────────────────

def _limpiar_nif(texto: str) -> str:
    """Normalizar NIF/CIF para validación: mayúsculas, sin espacios ni guiones ni puntos."""
    return re.sub(r"[\s\-\.]", "", texto.upper().strip())


# Patrones de fechas españolas y estándar
_PATRONES_FECHA = [
    # ISO: 2026-03-15
    (re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b'), "ISO"),
    # DD/MM/YYYY o DD-MM-YYYY
    (re.compile(r'\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b'), "DMY4"),
    # DD/MM/YY
    (re.compile(r'\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2})\b'), "DMY2"),
    # DDMMYYYY (sin separadores)
    (re.compile(r'\b(\d{2})(\d{2})(\d{4})\b'), "DDMMYYYY"),
]

_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}
_PATRON_FECHA_TEXTO = re.compile(
    r'\b(\d{1,2})\s+(?:de\s+)?(' + '|'.join(_MESES_ES.keys()) + r')\s+(?:de\s+)?(\d{4})\b',
    re.IGNORECASE
)

# Contexto léxico para NIF: después de la palabra clave, capturar patrón específico NIF/CIF/NIE
# Tres patrones posibles:
#   CIF: letra-tipo + 7 dígitos + control  → [ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-J]
#   NIF: 8 dígitos + letra                 → \d{8}[A-Z]
#   NIE: X/Y/Z + 7 dígitos + letra        → [XYZ]\d{7}[A-Z]
# Incluimos variantes con espacios inter-carácter por artefactos OCR (p.e. "B 12345674")
_PATRON_IDENTIFICADOR = re.compile(
    r'(?:[ABCDEFGHJKLMNPQRSUVW][ ]?\d{1,2}(?:[ ]?\d){5}[ ]?[0-9A-J]'  # CIF
    r'|\d{1,2}(?:[ ]?\d){6}[ ]?[A-Z]'                                    # NIF
    r'|[XYZ][ ]?\d{1,2}(?:[ ]?\d){5}[ ]?[A-Z])',                        # NIE
    re.IGNORECASE
)

_CONTEXTO_NIF = re.compile(
    r'(?:cif|nif|nie|n\.i\.f|n\.i\.e|c\.i\.f|tax[\s_]?id|número[\s_]?fiscal'
    r'|id[\s_]?fiscal|vat[\s_]?number|vat[\s_]?no)[.:)#\s]{1,5}',
    re.IGNORECASE
)

# Contexto léxico para NIF del receptor (cliente/destinatario)
_CONTEXTO_NIF_RECEPTOR = re.compile(
    r'(?:cliente|destinatario|receptor|bill\s*to|customer|ship\s*to'
    r'|datos\s+del\s+cliente|datos\s+fiscales\s+del?\s+cliente)[.:)#\s]{1,5}',
    re.IGNORECASE
)

# Contexto léxico para número de factura
# Separador permisivo: espacios, º, °, nº, puntos, coma, #, dos puntos
_CONTEXTO_NUM_FACTURA = re.compile(
    r'(?:factura|invoice|fra\.?|fac\.?|invoice\s+(?:no|number|num|#)'
    r'|ref(?:erencia)?|número\s+de\s+factura)'
    r'[.\s:)#nNºN°]{0,10}',
    re.IGNORECASE
)

# Contexto léxico para fechas
_CONTEXTO_FECHA_EXP = re.compile(
    r'(?:fecha\s+(?:de\s+)?(?:expedición|emisi[oó]n|factura|fact)'
    r'|invoice\s+date|date|fecha)[.:)\s]+',
    re.IGNORECASE
)


def _normalizar_fecha(texto: str) -> Optional[str]:
    """
    Normalizar cualquier representación de fecha a ISO 8601 (YYYY-MM-DD).
    Devuelve None si no se puede parsear o la fecha es incoherente.
    """
    t = texto.strip()

    # Texto ya en ISO
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    # DD/MM/YYYY o DD-MM-YYYY o DD.MM.YYYY
    m = re.match(r'^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})$', t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    # DD/MM/YY (año 2 dígitos)
    m = re.match(r'^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2})$', t)
    if m:
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + yy if yy < 50 else 1900 + yy
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    # DDMMYYYY sin separadores
    m = re.match(r'^(\d{2})(\d{2})(\d{4})$', t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None

    # "15 de marzo de 2026"
    m = _PATRON_FECHA_TEXTO.search(t)
    if m:
        d = int(m.group(1))
        mo = _MESES_ES.get(m.group(2).lower(), 0)
        y = int(m.group(3))
        if mo:
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                return None

    return None


def _candidato_desde_entity(
    entity: RawEntity,
    campo: str,
    fuente: FuenteCandidato,
    valor_normalizado: Optional[str] = None,
    motivo: str = "",
) -> FieldCandidate:
    """Construir un FieldCandidate a partir de una RawEntity de Document AI."""
    return FieldCandidate(
        valor_raw=entity.mention_text,
        valor_normalizado=valor_normalizado or entity.mention_text,
        fuente=fuente,
        confianza=entity.confidence,
        motivo=motivo or f"Campo nativo Document AI: {entity.entity_type}",
        page_ref=entity.page_number,
        bbox_normalizado=entity.bbox_normalizado,
        text_anchor_offsets=entity.text_anchor_offsets,
    )


def _decidir(candidatos: list[FieldCandidate], campo: str,
             umbral_auto: float = _UMBRAL_AUTO,
             umbral_warn: float = _UMBRAL_WARN) -> FieldResolution:
    """
    Elegir el mejor candidato viable y determinar la decisión.

    Orden de preferencia:
    1. Mayor confianza entre candidatos viables
    2. Fuente con mayor prioridad (Document AI > regex > LLM)
    3. Si ningún candidato es viable → BLOCK

    Nunca se elige un candidato con validacion_ok=False.
    """
    viables = [c for c in candidatos if c.es_viable()]

    if not viables:
        return FieldResolution(
            campo=campo,
            valor_final=None,
            fuente_final=None,
            confianza_final=0.0,
            decision=DecisionCampo.BLOCK,
            motivo="Sin candidatos válidos",
            candidatos=[_c_to_dict(c) for c in candidatos],
        )

    # Ordenar por confianza desc; en empate, por prioridad de fuente
    _prioridad = {
        FuenteCandidato.DOCUMENT_AI_NATIVO: 0,
        FuenteCandidato.NORMALIZED_VALUE: 1,
        FuenteCandidato.SISTEMA: 0,  # Misma prioridad que DOCUMENT_AI_NATIVO (valor del sistema)
        FuenteCandidato.FASE2_OCR: 2,        # Cloud Vision + Gemini (mejor que regex)
        FuenteCandidato.REGEX_CONTEXTUAL: 3,
        FuenteCandidato.BBOX_PROXIMIDAD: 4,
        FuenteCandidato.LLM_ARBITRO: 5,
        FuenteCandidato.FALLBACK: 6,
    }
    viables.sort(key=lambda c: (-c.confianza, _prioridad.get(c.fuente, 9)))
    elegido = viables[0]

    if elegido.confianza >= umbral_auto:
        dec = DecisionCampo.AUTO
        motivo = f"Candidato {elegido.fuente.value} con confianza {elegido.confianza:.2f}"
    elif elegido.confianza >= umbral_warn:
        dec = DecisionCampo.WARN
        motivo = (
            f"Candidato {elegido.fuente.value} con confianza {elegido.confianza:.2f} "
            f"(por debajo del umbral automático {umbral_auto})"
        )
    else:
        dec = DecisionCampo.BLOCK
        motivo = (
            f"Confianza insuficiente: {elegido.confianza:.2f} "
            f"(mínimo para revisión: {umbral_warn})"
        )

    return FieldResolution(
        campo=campo,
        valor_final=elegido.valor_normalizado,
        fuente_final=elegido.fuente,
        confianza_final=elegido.confianza,
        decision=dec,
        motivo=motivo,
        candidatos=[_c_to_dict(c) for c in candidatos],
        page_ref=elegido.page_ref,
        bbox_normalizado=elegido.bbox_normalizado,
    )


def _c_to_dict(c: FieldCandidate) -> dict:
    """Serializar FieldCandidate para el JSON de salida."""
    return {
        "valor_raw": c.valor_raw,
        "valor_normalizado": c.valor_normalizado,
        "fuente": c.fuente.value,
        "confianza": c.confianza,
        "motivo": c.motivo,
        "page_ref": c.page_ref,
        "bbox_normalizado": c.bbox_normalizado,
        "text_anchor_offsets": c.text_anchor_offsets,
        "validacion_ok": c.validacion_ok,
        "motivo_validacion": c.motivo_validacion,
    }


def _necesita_llm(resolution: FieldResolution) -> bool:
    """True si la resolución tiene confianza baja o conflicto entre candidatos."""
    if resolution.confianza_final < _UMBRAL_LLM:
        return True
    viables = [c for c in resolution.candidatos
               if c.get("validacion_ok") is not False and c.get("valor_normalizado")]
    # Conflicto: varios candidatos viables con valores distintos
    valores = list({c["valor_normalizado"] for c in viables if c.get("valor_normalizado")})
    return len(valores) > 1


def _candidatos_desde_fase2(documento_extraido: dict) -> dict:
    """
    Extraer candidatos de identificacion desde documento_extraido.json (Fase 2).

    Inyecta los valores extraídos por Cloud Vision + Gemini como candidatos con
    confianza 0.75 — por debajo de _UMBRAL_LLM=0.80, de modo que el LLM árbitro
    siempre los verifica antes de promover a decisión final.

    Returns:
        Dict con keys "nif_entidad", "nombre_entidad", "numero_factura",
        "fecha_expedicion". Valor None si el campo no está presente o vacío.
    """
    _CONFIANZA_FASE2 = 0.75

    resultado: dict = {
        "nif_entidad": None,
        "nombre_entidad": None,
        "numero_factura": None,
        "fecha_expedicion": None,
        "nif_receptor": None,
        "nombre_receptor": None,
    }

    if not documento_extraido:
        return resultado

    identificacion = documento_extraido.get("identificacion", {})

    # ── nif_entidad ──────────────────────────────────────────────────────
    nif_data = identificacion.get("nif_entidad", {})
    nif_valor = nif_data.get("valor") if nif_data else None
    if nif_valor:
        # Limpiar: quitar espacios, guiones, puntos y barras (formato Makro: "A-28/647451")
        nif_limpio = re.sub(r"[\s\-\./]", "", str(nif_valor).upper().strip())
        if len(nif_limpio) >= 7:
            validacion = validar_identificador_fiscal(nif_limpio)
            confianza_nif = _CONFIANZA_FASE2 if validacion.es_valido else _CONFIANZA_FASE2 * 0.3
            resultado["nif_entidad"] = FieldCandidate(
                valor_raw=str(nif_valor),
                valor_normalizado=nif_limpio,
                fuente=FuenteCandidato.FASE2_OCR,
                confianza=confianza_nif,
                motivo=f"Fase 2 OCR (checksum: {'OK' if validacion.es_valido else 'FALLA'})",
                validacion_ok=validacion.es_valido,
                motivo_validacion=validacion.razon,
            )

    # ── nombre_entidad ────────────────────────────────────────────────────
    nombre_data = identificacion.get("nombre_entidad", {})
    nombre_valor = nombre_data.get("valor") if nombre_data else None
    if nombre_valor and str(nombre_valor).strip():
        nombre_norm = str(nombre_valor).strip().upper()
        resultado["nombre_entidad"] = FieldCandidate(
            valor_raw=str(nombre_valor),
            valor_normalizado=nombre_norm,
            fuente=FuenteCandidato.FASE2_OCR,
            confianza=_CONFIANZA_FASE2,
            motivo="Fase 2 OCR (nombre_entidad)",
            validacion_ok=True,
        )

    # ── numero_factura ────────────────────────────────────────────────────
    num_data = identificacion.get("numero_factura", {})
    num_valor = num_data.get("valor") if num_data else None
    if num_valor and str(num_valor).strip():
        num_str = str(num_valor).strip()
        min_len = settings().numero_factura_min_length
        blacklist = settings().numero_factura_blacklist
        if len(num_str) >= min_len and num_str.upper() not in blacklist:
            resultado["numero_factura"] = FieldCandidate(
                valor_raw=num_str,
                valor_normalizado=num_str,
                fuente=FuenteCandidato.FASE2_OCR,
                confianza=_CONFIANZA_FASE2,
                motivo="Fase 2 OCR (numero_factura)",
                validacion_ok=True,
            )

    # ── fecha_expedicion ──────────────────────────────────────────────────
    fecha_data = identificacion.get("fecha_expedicion", {})
    fecha_valor = fecha_data.get("valor") if fecha_data else None
    if fecha_valor:
        fecha_norm = _normalizar_fecha(str(fecha_valor))
        if fecha_norm:
            resultado["fecha_expedicion"] = FieldCandidate(
                valor_raw=str(fecha_valor),
                valor_normalizado=fecha_norm,
                fuente=FuenteCandidato.FASE2_OCR,
                confianza=_CONFIANZA_FASE2,
                motivo="Fase 2 OCR (fecha_expedicion, formato ISO validado)",
                validacion_ok=True,
            )

    # ── nif_receptor ─────────────────────────────────────────────────────
    cliente_destino = documento_extraido.get("cliente_destino", {})

    nif_rec_data = cliente_destino.get("nif_receptor", {})
    nif_rec_valor = nif_rec_data.get("valor") if nif_rec_data else None
    if nif_rec_valor:
        nif_rec_limpio = re.sub(r"[\s\-\./]", "", str(nif_rec_valor).upper().strip())
        if len(nif_rec_limpio) >= 7:
            validacion = validar_identificador_fiscal(nif_rec_limpio)
            confianza_nif = _CONFIANZA_FASE2 if validacion.es_valido else _CONFIANZA_FASE2 * 0.3
            resultado["nif_receptor"] = FieldCandidate(
                valor_raw=str(nif_rec_valor),
                valor_normalizado=nif_rec_limpio,
                fuente=FuenteCandidato.FASE2_OCR,
                confianza=confianza_nif,
                motivo=f"Fase 2 OCR receptor (checksum: {'OK' if validacion.es_valido else 'FALLA'})",
                validacion_ok=validacion.es_valido,
                motivo_validacion=validacion.razon,
            )

    # ── nombre_receptor ──────────────────────────────────────────────────
    nombre_rec_data = cliente_destino.get("nombre_receptor", {})
    nombre_rec_valor = nombre_rec_data.get("valor") if nombre_rec_data else None
    if nombre_rec_valor and str(nombre_rec_valor).strip():
        nombre_rec_norm = str(nombre_rec_valor).strip().upper()
        resultado["nombre_receptor"] = FieldCandidate(
            valor_raw=str(nombre_rec_valor),
            valor_normalizado=nombre_rec_norm,
            fuente=FuenteCandidato.FASE2_OCR,
            confianza=_CONFIANZA_FASE2,
            motivo="Fase 2 OCR (nombre_receptor)",
            validacion_ok=True,
        )

    return resultado


# ──────────────────────────────────────────────────────────
# 1. NIF/CIF/NIE del emisor
# ──────────────────────────────────────────────────────────

def resolver_nif_entidad(
    extractor: DocumentAIEntityExtractor,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver nif_entidad con tres fuentes:
      1. supplier_tax_id (Document AI nativo) — fuente principal
      2. Regex contextual sobre texto OCR — fallback
      3. Fase 2 OCR (Cloud Vision + Gemini) — fuente suplementaria a conf=0.75

    Cada candidato pasa por checksum determinista NIF/CIF/NIE.
    Para NIF usamos mention_text (no normalized_value): Document AI no normaliza NIFs.
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: supplier_tax_id nativo ────────────────────────────────
    entity = extractor.extraer_primero("supplier_tax_id")
    if entity and entity.valor_para_nif:
        nif_limpio = _limpiar_nif(entity.valor_para_nif)
        validacion = validar_identificador_fiscal(nif_limpio)
        c = _candidato_desde_entity(
            entity, "nif_entidad", FuenteCandidato.DOCUMENT_AI_NATIVO,
            valor_normalizado=nif_limpio,
            motivo=f"supplier_tax_id Document AI (conf: {entity.confidence:.2f})",
        )
        c.validacion_ok = validacion.es_valido
        c.motivo_validacion = validacion.razon
        # Penalizar confianza si checksum falla (pero conservar el candidato para trazabilidad)
        if not validacion.es_valido:
            c.confianza = c.confianza * 0.3
            c.motivo += f" [checksum FALLA: {validacion.razon}]"
        candidatos.append(c)

    # ── Fuente 2: regex contextual sobre texto OCR ───────────────────────
    texto_ocr = extractor.texto_completo()
    if texto_ocr:
        for m_ctx in _CONTEXTO_NIF.finditer(texto_ocr):
            # Buscar un identificador fiscal específico en los ~20 chars siguientes
            resto = texto_ocr[m_ctx.end():m_ctx.end() + 20]
            m_id = _PATRON_IDENTIFICADOR.search(resto)
            if not m_id:
                continue
            nif_candidato = _limpiar_nif(m_id.group(0))
            if not nif_candidato or len(nif_candidato) < 7:
                continue
            # Evitar duplicado exacto del candidato 1
            if any(c.valor_normalizado == nif_candidato for c in candidatos):
                continue
            validacion = validar_identificador_fiscal(nif_candidato)
            confianza_regex = 0.70 if validacion.es_valido else 0.25
            c = FieldCandidate(
                valor_raw=m_id.group(0).strip(),
                valor_normalizado=nif_candidato,
                fuente=FuenteCandidato.REGEX_CONTEXTUAL,
                confianza=confianza_regex,
                motivo=f"Regex contextual CIF/NIF (checksum: {'OK' if validacion.es_valido else 'FALLA'})",
                validacion_ok=validacion.es_valido,
                motivo_validacion=validacion.razon,
            )
            candidatos.append(c)

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("nif_entidad"):
        c_f2 = candidatos_fase2["nif_entidad"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "nif_entidad", umbral_auto, umbral_warn)

    # Añadir detalle de validación: usar el valor final si existe, sino el primer candidato disponible
    nif_para_validar = res.valor_final or (
        candidatos[0].valor_normalizado if candidatos else None
    )
    if nif_para_validar:
        v = validar_identificador_fiscal(nif_para_validar)
        res.validaciones = {
            "tipo_identificador": v.tipo.value,
            "checksum_ok": v.checksum_ok,
            "formato_ok": v.formato_ok,
            "razon": v.razon,
        }

    logger.info(
        f"[nif_entidad] {res.decision.value} | "
        f"conf={res.confianza_final:.2f} | {len(candidatos)} candidatos"
    )
    return res


# ──────────────────────────────────────────────────────────
# 2. Nombre del emisor
# ──────────────────────────────────────────────────────────

def resolver_nombre_entidad(
    extractor: DocumentAIEntityExtractor,
    nif_resolution: Optional[FieldResolution] = None,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver nombre_entidad con tres fuentes:
      1. supplier_name (Document AI nativo)
      2. Texto próximo al bbox del NIF ya resuelto (coherencia espacial)
      3. Fase 2 OCR (Cloud Vision + Gemini) — fuente suplementaria a conf=0.75

    El nombre no tiene checksum; la coherencia con el NIF es la validación.
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: supplier_name nativo ──────────────────────────────────
    entity = extractor.extraer_primero("supplier_name")
    if entity and entity.valor_preferido:
        nombre_norm = entity.valor_preferido.strip().upper()
        c = _candidato_desde_entity(
            entity, "nombre_entidad", FuenteCandidato.DOCUMENT_AI_NATIVO,
            valor_normalizado=nombre_norm,
            motivo=f"supplier_name Document AI (conf: {entity.confidence:.2f})",
        )
        c.validacion_ok = True  # Sin checksum para nombre
        candidatos.append(c)

    # ── Fuente 2: normalized_value si difiere del mention_text ──────────
    if entity and entity.normalized_text and entity.normalized_text != entity.mention_text:
        nombre_norm2 = entity.normalized_text.strip().upper()
        if not any(c.valor_normalizado == nombre_norm2 for c in candidatos):
            c2 = FieldCandidate(
                valor_raw=entity.normalized_text,
                valor_normalizado=nombre_norm2,
                fuente=FuenteCandidato.NORMALIZED_VALUE,
                confianza=entity.confidence * 0.95,  # Leve descuento por ser derivado
                motivo="normalized_value de supplier_name",
                page_ref=entity.page_number,
                bbox_normalizado=entity.bbox_normalizado,
                text_anchor_offsets=entity.text_anchor_offsets,
                validacion_ok=True,
            )
            candidatos.append(c2)

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("nombre_entidad"):
        c_f2 = candidatos_fase2["nombre_entidad"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "nombre_entidad", umbral_auto, umbral_warn)

    # Coherencia espacial: si el nombre está en la misma página que el NIF, boost
    if (nif_resolution and nif_resolution.page_ref is not None
            and res.page_ref is not None
            and nif_resolution.page_ref == res.page_ref):
        res.validaciones["coherencia_pagina_nif"] = True

    logger.info(
        f"[nombre_entidad] {res.decision.value} | "
        f"conf={res.confianza_final:.2f}"
    )
    return res


# ──────────────────────────────────────────────────────────
# 3. Número de factura
# ──────────────────────────────────────────────────────────

def resolver_numero_factura(
    extractor: DocumentAIEntityExtractor,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver numero_factura con tres fuentes:
      1. invoice_id (Document AI nativo)
      2. Regex contextual con palabras clave (factura, invoice, fra, nº)
      3. Fase 2 OCR (Cloud Vision + Gemini) — fuente suplementaria a conf=0.75

    No se usa regex ciega sobre cualquier patrón alfanumérico — solo con contexto.
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: invoice_id nativo ──────────────────────────────────────
    entity = extractor.extraer_primero("invoice_id")
    if entity and entity.valor_preferido:
        num = entity.valor_preferido.strip()
        c = _candidato_desde_entity(
            entity, "numero_factura", FuenteCandidato.DOCUMENT_AI_NATIVO,
            valor_normalizado=num,
            motivo=f"invoice_id Document AI (conf: {entity.confidence:.2f})",
        )
        c.validacion_ok = True  # Sin formato obligatorio para número de factura
        candidatos.append(c)

    # ── Fuente 2: regex contextual ───────────────────────────────────────
    texto_ocr = extractor.texto_completo()
    if texto_ocr:
        for m_ctx in _CONTEXTO_NUM_FACTURA.finditer(texto_ocr):
            # Extraer el número en los ~30 chars siguientes al contexto
            resto = texto_ocr[m_ctx.end():m_ctx.end() + 30]
            # Número de factura: alfanumérico con separadores / - _ \
            m_num = re.search(r'([A-Z0-9][A-Z0-9\-/\\_]{1,28})', resto, re.IGNORECASE)
            if not m_num:
                continue
            num_candidato = m_num.group(1).strip()

            # Filtro 1: longitud mínima (configurable)
            if len(num_candidato) < settings().numero_factura_min_length:
                continue

            # Filtro 2: blacklist de palabras comunes (configurable)
            if num_candidato.upper() in settings().numero_factura_blacklist:
                continue

            # Filtro 3: deduplicación
            if any(c.valor_normalizado == num_candidato for c in candidatos):
                continue
            c = FieldCandidate(
                valor_raw=num_candidato,
                valor_normalizado=num_candidato,
                fuente=FuenteCandidato.REGEX_CONTEXTUAL,
                confianza=0.65,
                motivo=f"Regex contextual número de factura (contexto: '...{m_ctx.group(0)[:30]}...')",
                validacion_ok=True,
            )
            candidatos.append(c)

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("numero_factura"):
        c_f2 = candidatos_fase2["numero_factura"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "numero_factura", umbral_auto, umbral_warn)
    logger.info(
        f"[numero_factura] {res.decision.value} | "
        f"conf={res.confianza_final:.2f}"
    )
    return res


# ──────────────────────────────────────────────────────────
# 4. Fechas (expedición, vencimiento, operación)
# ──────────────────────────────────────────────────────────

def _resolver_fecha_generico(
    entity_types: list[str],
    campo: str,
    extractor: DocumentAIEntityExtractor,
    patron_contexto: re.Pattern,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
) -> FieldResolution:
    """
    Resolver una fecha con la secuencia canónica:
      1. Buscar en TODOS los entity_types de Document AI (invoice_date, delivery_date, etc.)
         → preferir normalized_value (ISO 8601) para cada uno
      2. Regex contextual sobre texto OCR

    Recolecta todos los candidatos de fecha disponibles en el documento para que
    el LLM árbitro pueda decidir cuál es la correcta según el contexto.

    Normalización final: siempre ISO 8601 (YYYY-MM-DD).
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: buscar en TODOS los entity_types proporcionados ───────────
    for entity_type in entity_types:
        entity = extractor.extraer_primero(entity_type)
        if not entity:
            continue

        # Preferir normalized_text (Document AI ya devuelve ISO en fechas)
        fecha_norm = None
        fuente_fecha = FuenteCandidato.DOCUMENT_AI_NATIVO

        if entity.normalized_text:
            fecha_norm = _normalizar_fecha(entity.normalized_text)
            fuente_fecha = FuenteCandidato.NORMALIZED_VALUE

        if not fecha_norm and entity.mention_text:
            fecha_norm = _normalizar_fecha(entity.mention_text)
            fuente_fecha = FuenteCandidato.DOCUMENT_AI_NATIVO

        if fecha_norm:
            # Evitar duplicados exactos (mismo valor normalizado)
            if any(c.valor_normalizado == fecha_norm for c in candidatos):
                continue

            c = _candidato_desde_entity(
                entity, campo, fuente_fecha,
                valor_normalizado=fecha_norm,
                motivo=(
                    f"{entity_type} Document AI, "
                    f"{'normalized_value' if fuente_fecha == FuenteCandidato.NORMALIZED_VALUE else 'mention_text'} "
                    f"(conf: {entity.confidence:.2f})"
                ),
            )
            c.validacion_ok = True
            candidatos.append(c)

    # ── Fuente 2: regex contextual ───────────────────────────────────────
    texto_ocr = extractor.texto_completo()
    if texto_ocr:
        for m in patron_contexto.finditer(texto_ocr):
            # Tomar el texto justo después del contexto y buscar una fecha
            resto = texto_ocr[m.end():m.end() + 40]
            for patron_f, _formato in _PATRONES_FECHA:
                mf = patron_f.search(resto)
                if mf:
                    fecha_raw = mf.group(0)
                    fecha_norm = _normalizar_fecha(fecha_raw)
                    if not fecha_norm:
                        continue
                    if any(c.valor_normalizado == fecha_norm for c in candidatos):
                        break
                    c = FieldCandidate(
                        valor_raw=fecha_raw,
                        valor_normalizado=fecha_norm,
                        fuente=FuenteCandidato.REGEX_CONTEXTUAL,
                        confianza=0.68,
                        motivo=f"Regex contextual con contexto '{m.group(0)[:30]}...'",
                        validacion_ok=True,
                    )
                    candidatos.append(c)
                    break

    res = _decidir(candidatos, campo, umbral_auto, umbral_warn)
    logger.info(
        f"[{campo}] {res.decision.value} | "
        f"conf={res.confianza_final:.2f}"
    )
    return res


def resolver_fecha_expedicion(
    extractor: DocumentAIEntityExtractor,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver fecha_expedicion con cascade de prioridad y penalización de confianza.

    Busca en múltiples entity_types con prioridades:
    1. invoice_date (fecha de factura, preferida, sin penalización)
    2. delivery_date (fecha de entrega, fallback, confianza × 0.85)
    3. receive_date (fecha de recepción, último recurso, confianza × 0.70)
    4. due_date (fecha de vencimiento, último recurso, confianza × 0.70)

    La penalización de confianza permite que documentos sin invoice_date explícito
    usen fechas alternativas con confianza reducida (WARN en lugar de BLOCK),
    evitando bloqueos innecesarios.

    Si hay múltiples candidatos viables, el LLM árbitro decidirá cuál es
    la fecha de expedición correcta basándose en el contexto del documento.
    """
    # Prioridades y penalizaciones por tipo de fecha
    ENTITY_PRIORITY = [
        ("invoice_date", 1.0, "Fecha de factura (entity nativo)"),
        ("delivery_date", 0.85, "Fecha de entrega (fallback, confianza reducida)"),
        ("receive_date", 0.70, "Fecha de recepción (último recurso)"),
        ("due_date", 0.70, "Fecha de vencimiento (último recurso)"),
    ]

    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: buscar en entity_types con penalización ───────────
    for entity_type, penalizacion, descripcion in ENTITY_PRIORITY:
        entity = extractor.extraer_primero(entity_type)
        if not entity:
            continue

        # Preferir normalized_text (Document AI ya devuelve ISO en fechas)
        fecha_norm = None
        fuente_fecha = FuenteCandidato.DOCUMENT_AI_NATIVO

        if entity.normalized_text:
            fecha_norm = _normalizar_fecha(entity.normalized_text)
            fuente_fecha = FuenteCandidato.NORMALIZED_VALUE

        if not fecha_norm and entity.mention_text:
            fecha_norm = _normalizar_fecha(entity.mention_text)
            fuente_fecha = FuenteCandidato.DOCUMENT_AI_NATIVO

        if fecha_norm:
            # Evitar duplicados exactos
            if any(c.valor_normalizado == fecha_norm for c in candidatos):
                continue

            # Aplicar penalización a la confianza
            confianza_ajustada = entity.confidence * penalizacion

            c = _candidato_desde_entity(
                entity,
                "fecha_expedicion",
                fuente_fecha,
                valor_normalizado=fecha_norm,
                motivo=f"{descripcion} (conf: {entity.confidence:.2f} × {penalizacion} = {confianza_ajustada:.2f})",
            )
            c.confianza = confianza_ajustada
            c.validacion_ok = True
            candidatos.append(c)

    # ── Fuente 2: regex contextual ───────────────────────────────────────
    texto_ocr = extractor.texto_completo()
    if texto_ocr:
        for m in _CONTEXTO_FECHA_EXP.finditer(texto_ocr):
            # Tomar el texto justo después del contexto y buscar una fecha
            resto = texto_ocr[m.end() : m.end() + 40]
            for patron_f, _formato in _PATRONES_FECHA:
                mf = patron_f.search(resto)
                if mf:
                    fecha_raw = mf.group(0)
                    fecha_norm = _normalizar_fecha(fecha_raw)
                    if not fecha_norm:
                        continue
                    if any(c.valor_normalizado == fecha_norm for c in candidatos):
                        break
                    c = FieldCandidate(
                        valor_raw=fecha_raw,
                        valor_normalizado=fecha_norm,
                        fuente=FuenteCandidato.REGEX_CONTEXTUAL,
                        confianza=0.68,
                        motivo=f"Regex contextual con contexto '{m.group(0)[:30]}...'",
                        validacion_ok=True,
                    )
                    candidatos.append(c)
                    break

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("fecha_expedicion"):
        c_f2 = candidatos_fase2["fecha_expedicion"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "fecha_expedicion", umbral_auto, umbral_warn)
    logger.info(f"[fecha_expedicion] {res.decision.value} | conf={res.confianza_final:.2f}")
    return res


def resolver_fecha_operacion(
    res_fecha_exp: Optional[FieldResolution] = None,
) -> FieldResolution:
    """
    Fecha de operación: se deriva de la fecha de expedición resuelta.

    La mayoría de facturas no traen fecha de operación explícita. Fabricarla con
    la fecha de proceso del sistema (hoy) y confianza 1.0 viola el principio "no
    inventar datos" (hallazgo F3). En su lugar:
      - Si hay fecha_expedicion resuelta → copiar su valor, confianza y decisión
        (fuente DERIVADO), nunca AUTO 1.0 sobre una fecha del sistema.
      - Si no hay expedición → valor None y decisión PENDIENTE (campo no obligatorio).

    fecha_operacion no es un campo obligatorio: no afecta la decisión global.
    """
    if res_fecha_exp is not None and res_fecha_exp.valor_final:
        valor = res_fecha_exp.valor_final
        c = FieldCandidate(
            valor_raw=valor,
            valor_normalizado=valor,
            fuente=FuenteCandidato.DERIVADO,
            confianza=res_fecha_exp.confianza_final,
            motivo="Derivado de fecha_expedicion (la factura no aporta fecha de operación)",
            validacion_ok=True,
        )
        return FieldResolution(
            campo="fecha_operacion",
            valor_final=valor,
            fuente_final=FuenteCandidato.DERIVADO,
            confianza_final=res_fecha_exp.confianza_final,
            decision=res_fecha_exp.decision,
            motivo="Fecha de operación derivada de fecha_expedicion",
            candidatos=[_c_to_dict(c)],
        )

    return FieldResolution(
        campo="fecha_operacion",
        valor_final=None,
        fuente_final=None,
        confianza_final=0.0,
        decision=DecisionCampo.PENDIENTE,
        motivo="Sin fecha de expedición — fecha de operación pendiente (no se fabrica)",
        candidatos=[],
    )


# ──────────────────────────────────────────────────────────
# 5. NIF/CIF/NIE del receptor
# ──────────────────────────────────────────────────────────

def resolver_nif_receptor(
    extractor: DocumentAIEntityExtractor,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver nif_receptor con tres fuentes:
      1. receiver_tax_id (Document AI nativo) — fuente principal
      2. Regex contextual sobre texto OCR con contexto de receptor — fallback
      3. Fase 2 OCR (Cloud Vision + Gemini) — fuente suplementaria a conf=0.75

    Cada candidato pasa por checksum determinista NIF/CIF/NIE.
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: receiver_tax_id nativo ────────────────────────────────
    entity = extractor.extraer_primero("receiver_tax_id")
    if entity and entity.valor_para_nif:
        nif_limpio = _limpiar_nif(entity.valor_para_nif)
        validacion = validar_identificador_fiscal(nif_limpio)
        c = _candidato_desde_entity(
            entity, "nif_receptor", FuenteCandidato.DOCUMENT_AI_NATIVO,
            valor_normalizado=nif_limpio,
            motivo=f"receiver_tax_id Document AI (conf: {entity.confidence:.2f})",
        )
        c.validacion_ok = validacion.es_valido
        c.motivo_validacion = validacion.razon
        if not validacion.es_valido:
            c.confianza = c.confianza * 0.3
            c.motivo += f" [checksum FALLA: {validacion.razon}]"
        candidatos.append(c)

    # ── Fuente 2: regex contextual sobre texto OCR ───────────────────────
    texto_ocr = extractor.texto_completo()
    if texto_ocr:
        for m_ctx in _CONTEXTO_NIF_RECEPTOR.finditer(texto_ocr):
            resto = texto_ocr[m_ctx.end():m_ctx.end() + 20]
            m_id = _PATRON_IDENTIFICADOR.search(resto)
            if not m_id:
                continue
            nif_candidato = _limpiar_nif(m_id.group(0))
            if not nif_candidato or len(nif_candidato) < 7:
                continue
            if any(c.valor_normalizado == nif_candidato for c in candidatos):
                continue
            validacion = validar_identificador_fiscal(nif_candidato)
            confianza_regex = 0.70 if validacion.es_valido else 0.25
            c = FieldCandidate(
                valor_raw=m_id.group(0).strip(),
                valor_normalizado=nif_candidato,
                fuente=FuenteCandidato.REGEX_CONTEXTUAL,
                confianza=confianza_regex,
                motivo=f"Regex contextual receptor CIF/NIF (checksum: {'OK' if validacion.es_valido else 'FALLA'})",
                validacion_ok=validacion.es_valido,
                motivo_validacion=validacion.razon,
            )
            candidatos.append(c)

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("nif_receptor"):
        c_f2 = candidatos_fase2["nif_receptor"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "nif_receptor", umbral_auto, umbral_warn)

    nif_para_validar = res.valor_final or (
        candidatos[0].valor_normalizado if candidatos else None
    )
    if nif_para_validar:
        v = validar_identificador_fiscal(nif_para_validar)
        res.validaciones = {
            "tipo_identificador": v.tipo.value,
            "checksum_ok": v.checksum_ok,
            "formato_ok": v.formato_ok,
            "razon": v.razon,
        }

    logger.info(
        f"[nif_receptor] {res.decision.value} | "
        f"conf={res.confianza_final:.2f} | {len(candidatos)} candidatos"
    )
    return res


# ──────────────────────────────────────────────────────────
# 6. Nombre del receptor
# ──────────────────────────────────────────────────────────

def resolver_nombre_receptor(
    extractor: DocumentAIEntityExtractor,
    nif_receptor_resolution: Optional[FieldResolution] = None,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
    candidatos_fase2: Optional[dict] = None,
) -> FieldResolution:
    """
    Resolver nombre_receptor con tres fuentes:
      1. receiver_name (Document AI nativo)
      2. normalized_value si difiere del mention_text
      3. Fase 2 OCR (Cloud Vision + Gemini) — fuente suplementaria a conf=0.75

    Coherencia espacial: si el nombre está en la misma página que el NIF receptor, boost.
    """
    candidatos: list[FieldCandidate] = []

    # ── Fuente 1: receiver_name nativo ──────────────────────────────────
    entity = extractor.extraer_primero("receiver_name")
    if entity and entity.valor_preferido:
        nombre_norm = entity.valor_preferido.strip().upper()
        c = _candidato_desde_entity(
            entity, "nombre_receptor", FuenteCandidato.DOCUMENT_AI_NATIVO,
            valor_normalizado=nombre_norm,
            motivo=f"receiver_name Document AI (conf: {entity.confidence:.2f})",
        )
        c.validacion_ok = True
        candidatos.append(c)

    # ── Fuente 2: normalized_value si difiere del mention_text ──────────
    if entity and entity.normalized_text and entity.normalized_text != entity.mention_text:
        nombre_norm2 = entity.normalized_text.strip().upper()
        if not any(c.valor_normalizado == nombre_norm2 for c in candidatos):
            c2 = FieldCandidate(
                valor_raw=entity.normalized_text,
                valor_normalizado=nombre_norm2,
                fuente=FuenteCandidato.NORMALIZED_VALUE,
                confianza=entity.confidence * 0.95,
                motivo="normalized_value de receiver_name",
                page_ref=entity.page_number,
                bbox_normalizado=entity.bbox_normalizado,
                text_anchor_offsets=entity.text_anchor_offsets,
                validacion_ok=True,
            )
            candidatos.append(c2)

    # ── Fuente 3: candidato de Fase 2 OCR ───────────────────────────────
    if candidatos_fase2 and candidatos_fase2.get("nombre_receptor"):
        c_f2 = candidatos_fase2["nombre_receptor"]
        if not any(c.valor_normalizado == c_f2.valor_normalizado for c in candidatos):
            candidatos.append(c_f2)

    res = _decidir(candidatos, "nombre_receptor", umbral_auto, umbral_warn)

    # Coherencia espacial: si el nombre está en la misma página que el NIF receptor, boost
    if (nif_receptor_resolution and nif_receptor_resolution.page_ref is not None
            and res.page_ref is not None
            and nif_receptor_resolution.page_ref == res.page_ref):
        res.validaciones["coherencia_pagina_nif_receptor"] = True

    logger.info(
        f"[nombre_receptor] {res.decision.value} | "
        f"conf={res.confianza_final:.2f}"
    )
    return res
