"""
Contratos de datos para el resolvedor de cabecera e identidad.

Cada campo se resuelve de forma independiente produciendo una FieldResolution
con lista de candidatos, scoring y decisión final. El LLM solo actúa como
árbitro cuando hay conflicto o baja confianza — nunca como fuente primaria.

Contrato de salida: resultado_identidad_cabecera.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Any


# ──────────────────────────────────────────────────────────
# Fuentes de candidatos (orden de prioridad)
# ──────────────────────────────────────────────────────────

class FuenteCandidato(str, Enum):
    """Origen de un candidato. Los valores de menor número tienen mayor prioridad."""
    DOCUMENT_AI_NATIVO    = "document_ai_nativo"     # supplier_tax_id, invoice_id, etc.
    NORMALIZED_VALUE      = "normalized_value"        # normalized_value del entity
    FASE2_OCR             = "fase2_ocr"              # Cloud Vision + Gemini 2.5 Flash (fase 2)
    REGEX_CONTEXTUAL      = "regex_contextual"        # regex sobre texto OCR con contexto
    BBOX_PROXIMIDAD       = "bbox_proximidad"         # texto próximo a un bbox conocido
    LLM_ARBITRO           = "llm_arbitro"             # Gemini como árbitro entre candidatos
    SISTEMA               = "sistema"                 # Generado por el sistema (fecha operación)
    DERIVADO              = "derivado"                # Derivado de otro campo resuelto (p.ej. fecha_operacion ← fecha_expedicion)
    FALLBACK              = "fallback"                # último recurso sin contexto


class DecisionCampo(str, Enum):
    AUTO      = "auto"      # único candidato válido con confianza suficiente
    WARN      = "warn"      # candidato elegido pero con incertidumbre
    PENDIENTE = "pendiente"  # dato ausente: no se fabrica (campo no obligatorio)
    BLOCK     = "block"     # sin candidato válido o conflicto no resuelto


# ──────────────────────────────────────────────────────────
# Candidato individual
# ──────────────────────────────────────────────────────────

@dataclass
class FieldCandidate:
    """
    Un candidato para el valor de un campo de cabecera.

    Cada candidato viene de una fuente específica (Document AI nativo,
    regex contextual, LLM árbitro...) y lleva su propia confianza,
    valor normalizado y trazabilidad de ubicación.
    """
    valor_raw: Optional[str]          # Texto exacto extraído (mention_text o regex match)
    valor_normalizado: Optional[str]  # Valor después de normalización (ISO, mayúsculas, etc.)
    fuente: FuenteCandidato
    confianza: float                  # 0.0–1.0
    motivo: str                       # Por qué se propone este candidato

    # Trazabilidad de ubicación (del pageAnchor de Document AI)
    page_ref: Optional[int] = None          # Número de página (0-indexed)
    bbox_normalizado: Optional[dict] = None  # {x_min,y_min,x_max,y_max} en [0,1]

    # Trazabilidad de texto (del textAnchor de Document AI)
    text_anchor_offsets: Optional[list] = None  # [{startIndex, endIndex}]

    # Si este candidato pasó validación determinista (checksum, formato, etc.)
    validacion_ok: Optional[bool] = None
    motivo_validacion: Optional[str] = None

    def es_viable(self) -> bool:
        """Un candidato es viable si tiene valor y, si tiene validación, la pasó."""
        if not self.valor_normalizado:
            return False
        if self.validacion_ok is not None and not self.validacion_ok:
            return False
        return True


# ──────────────────────────────────────────────────────────
# Resolución de un campo
# ──────────────────────────────────────────────────────────

@dataclass
class FieldResolution:
    """
    Resolución completa de un campo de cabecera.

    Contiene la lista de candidatos evaluados, el elegido y la decisión final.
    Diseñado para ser directamente serializable a JSON en resultado_identidad_cabecera.json.
    """
    campo: str                              # "nif_entidad", "numero_factura", etc.
    valor_final: Optional[str]              # Valor listo para usar en el asiento
    fuente_final: Optional[FuenteCandidato]
    confianza_final: float
    decision: DecisionCampo
    motivo: str                             # Razón legible de la decisión

    # Trazabilidad completa
    candidatos: list = field(default_factory=list)  # Lista de FieldCandidate
    page_ref: Optional[int] = None
    bbox_normalizado: Optional[dict] = None

    # Validaciones específicas del campo (checksum NIF, formato fecha, etc.)
    validaciones: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convertir Enums a strings
        if d.get("fuente_final"):
            d["fuente_final"] = self.fuente_final.value if self.fuente_final else None
        d["decision"] = self.decision.value
        for c in d.get("candidatos", []):
            if isinstance(c.get("fuente"), FuenteCandidato):
                c["fuente"] = c["fuente"].value
            elif isinstance(c.get("fuente"), str):
                pass  # asdict ya convirtió el Enum a valor
        return d


# ──────────────────────────────────────────────────────────
# Resultado completo de cabecera
# ──────────────────────────────────────────────────────────

@dataclass
class CabeceraResult:
    """
    Resultado completo de la fase 3: identidad y cabecera.

    Salida → resultado_identidad_cabecera.json
    Contiene un FieldResolution por cada campo de cabecera fiscal.
    """
    documento_id: str
    fase: str = "3_identidad_cabecera"
    version_politica: str = "v1"
    timestamp: str = ""

    # Campos de cabecera — cada uno es una FieldResolution completa
    nif_entidad:      Optional[dict] = None   # serializado con to_dict()
    nombre_entidad:   Optional[dict] = None
    numero_factura:   Optional[dict] = None
    fecha_expedicion: Optional[dict] = None
    fecha_operacion:  Optional[dict] = None   # siempre presente: fecha de registro
    nif_receptor:     Optional[dict] = None   # NIF/CIF del receptor de la factura
    nombre_receptor:  Optional[dict] = None   # Razón social del receptor

    # Decisión global agregada
    decision_global: str = DecisionCampo.BLOCK.value
    requiere_revision_humana: bool = True
    motivos_revision: list = field(default_factory=list)

    # Metadatos
    llm_usado: bool = False        # True si se llamó al LLM árbitro en algún campo
    tokens_llm: int = 0            # Total tokens LLM consumidos

    def __post_init__(self):
        if not self.timestamp:
            from datetime import datetime, timezone
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def _decision_global(self) -> DecisionCampo:
        """
        La decisión global es la peor decisión entre todos los campos obligatorios.
        Campos opcionales (fecha_vencimiento) no afectan la decisión global.
        """
        campos_obligatorios = [
            self.nif_entidad,
            self.nombre_entidad,
            self.numero_factura,
            self.fecha_expedicion,
            self.nif_receptor,
            self.nombre_receptor,
        ]
        decisiones = [
            c.get("decision", DecisionCampo.BLOCK.value)
            for c in campos_obligatorios
            if c is not None
        ]
        if DecisionCampo.BLOCK.value in decisiones:
            return DecisionCampo.BLOCK
        if DecisionCampo.WARN.value in decisiones:
            return DecisionCampo.WARN
        return DecisionCampo.AUTO

    def finalizar(self) -> "CabeceraResult":
        """Calcular decisión global y marcar si requiere revisión humana."""
        dec = self._decision_global()
        self.decision_global = dec.value
        self.requiere_revision_humana = (dec != DecisionCampo.AUTO)
        return self

    def to_dict(self) -> dict:
        return {
            "documento_id": self.documento_id,
            "fase": self.fase,
            "version_politica": self.version_politica,
            "timestamp": self.timestamp,
            "campos": {
                "nif_entidad":      self.nif_entidad,
                "nombre_entidad":   self.nombre_entidad,
                "numero_factura":   self.numero_factura,
                "fecha_expedicion": self.fecha_expedicion,
                "fecha_operacion":  self.fecha_operacion,
                "nif_receptor":     self.nif_receptor,
                "nombre_receptor":  self.nombre_receptor,
            },
            "decision_global": self.decision_global,
            "requiere_revision_humana": self.requiere_revision_humana,
            "motivos_revision": self.motivos_revision,
            "llm_usado": self.llm_usado,
            "tokens_llm": self.tokens_llm,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
