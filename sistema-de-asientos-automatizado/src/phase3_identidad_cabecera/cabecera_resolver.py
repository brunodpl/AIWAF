"""
Orquestador de la fase 3: Identidad y Cabecera.

Secuencia de ejecución por campo:
  1. Ejecutar resolvedor de campo (DocAI nativo → regex contextual)
  2. Evaluar si el resultado necesita LLM (conflicto o baja confianza)
  3. Si LLM: llamar árbitro, elegir candidato, REVALIDAR con checksum/fecha
  4. Producir resultado_identidad_cabecera.json con trazabilidad completa

Principio de diseño:
  - El LLM árbitro solo elige entre candidatos ya extraídos — no inventa valores
  - Toda corrección LLM pasa por re-validación determinista antes de ser final
  - Campos opcionales (fecha_vencimiento) no bloquean el pipeline si ausentes
  - La decisión global es la peor decisión entre los campos obligatorios

Salida: resultado_identidad_cabecera.json
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from typing import Optional

from .docai_extractor import DocumentAIEntityExtractor
from .field_candidate import (
    CabeceraResult, DecisionCampo, FieldResolution, FuenteCandidato
)
from .field_resolvers import (
    resolver_nif_entidad,
    resolver_nombre_entidad,
    resolver_numero_factura,
    resolver_fecha_expedicion,
    resolver_fecha_operacion,
    resolver_nif_receptor,
    resolver_nombre_receptor,
    _candidatos_desde_fase2,
)
from .nif_cif_validator import validar_identificador_fiscal
from .llm_disambiguator import LLMDisambiguator

logger = logging.getLogger("pipeline.identidad")


class CabeceraResolver:
    """
    Resolvedor de cabecera e identidad para la fase 3.

    Produce resultado_identidad_cabecera.json con un FieldResolution
    auditado por campo.
    """

    def __init__(
        self,
        usar_llm: bool = True,
        project: Optional[str] = None,
        credentials_path: Optional[str] = None,
        gemini_model: str = "",
        gemini_location: str = "",
        gemini_max_retries: int = 2,
        umbral_auto: float = 0.90,
        umbral_warn: float = 0.65,
        umbral_llm: float = 0.80,
        autofactura_marcadores: Optional[list[str]] = None,
        autofactura_swap_enabled: bool = True,
        operadora_nifs: Optional[set[str]] = None,
    ):
        """
        Args:
            usar_llm: Si False, nunca se llama al LLM (modo puro determinista).
            project: GCP project ID for Vertex AI.
            credentials_path: Ruta al JSON de service account para Vertex AI.
            gemini_model: Modelo Gemini para el árbitro LLM.
            gemini_location: Región de Vertex AI para el LLM.
            gemini_max_retries: Reintentos máximos en llamadas al LLM.
            umbral_auto: Confianza mínima para decisión AUTO sin revisión.
            umbral_warn: Por debajo → BLOCK.
            umbral_llm: Por debajo de este umbral, se considera llamar al LLM árbitro.
            autofactura_marcadores: Marcadores de "facturación por el destinatario"
                que disparan la inversión emisor/receptor. Si vacío/None → desactivado.
            autofactura_swap_enabled: Si False, nunca se invierte (válvula de emergencia).
            operadora_nifs: NIF de operadoras de máquinas recreativas. Si el NIF
                en rol de cliente para el libro es una operadora, se invierte
                emisor/receptor de forma determinista (sin depender de marcador).
        """
        self.usar_llm = usar_llm
        self.umbral_auto = umbral_auto
        self.umbral_warn = umbral_warn
        self.umbral_llm = umbral_llm

        self._autofactura_marcadores = autofactura_marcadores or []
        self._operadora_nifs = operadora_nifs or set()
        # El swap se activa si hay marcadores O registro de operadoras (cualquiera
        # de los dos disparadores). La válvula maestra sigue siendo el flag.
        self._autofactura_enabled = autofactura_swap_enabled and (
            bool(self._autofactura_marcadores) or bool(self._operadora_nifs)
        )

        self._llm: Optional[LLMDisambiguator] = None
        if usar_llm:
            if not gemini_model or not gemini_location or not project:
                raise ValueError(
                    "gemini_model, gemini_location y project son obligatorios cuando usar_llm=True. "
                    "Verifica que src.config.settings() se carga correctamente desde .env."
                )
            try:
                self._llm = LLMDisambiguator(
                    project=project,
                    credentials_path=credentials_path,
                    location=gemini_location,
                    model_name=gemini_model,
                    max_retries=gemini_max_retries,
                )
            except ImportError:
                logger.warning("[cabecera] google-generativeai no disponible — LLM desactivado")
                self._llm = None

        self._total_tokens_llm = 0

    def _aplicar_llm_si_necesario(
        self,
        resolution: FieldResolution,
        extractor: DocumentAIEntityExtractor,
        raw_document_ai: dict,
    ) -> FieldResolution:
        """
        Si el campo tiene conflicto o baja confianza, llamar al LLM árbitro.

        El LLM solo elige entre candidatos ya extraídos. Su elección pasa
        obligatoriamente por re-validación antes de convertirse en valor final.

        Returns:
            FieldResolution (posiblemente actualizada con elección del LLM)
        """
        if self._llm is None or not self.usar_llm:
            return resolution

        if not _necesita_llm_con_umbral(resolution, self.umbral_llm):
            return resolution

        campo = resolution.campo
        candidatos = resolution.candidatos
        if not candidatos:
            return resolution

        # Guard universal: si solo hay 1 candidato y tiene validacion_ok=False,
        # el LLM no puede mejorar el resultado → skip para ahorrar tokens
        if len(candidatos) == 1:
            if all(not c.get("validacion_ok", True) for c in candidatos):
                logger.info(
                    f"[cabecera] Skipping LLM para '{campo}': "
                    f"único candidato con validacion_ok=False — no hay elección posible"
                )
                return resolution

        logger.info(f"[cabecera] LLM árbitro invocado para '{campo}' (conf={resolution.confianza_final:.2f})")

        # Dar al LLM fragmento OCR contextual relevante
        texto_ocr = extractor.texto_completo()
        contexto = _extraer_contexto_relevante(texto_ocr, campo, candidatos=candidatos, max_chars=300)

        # Extraer crop visual del mejor candidato si tiene bbox
        imagen_crop = None
        if candidatos and candidatos[0].get("bbox_normalizado") and candidatos[0].get("page_ref") is not None:
            bbox = candidatos[0]["bbox_normalizado"]
            page = candidatos[0]["page_ref"]
            imagen_crop = _extraer_crop_region(raw_document_ai, bbox, page)

        arbitraje = self._llm.arbitrar(campo, candidatos, contexto, imagen_crop=imagen_crop)
        self._total_tokens_llm += arbitraje.tokens_prompt + arbitraje.tokens_respuesta

        if arbitraje.error or arbitraje.indice_elegido is None:
            # Error técnico o LLM no pudo decidir → marcar en motivo, no cambiar decisión
            resolution.motivo += f" | LLM árbitro: {arbitraje.justificacion}"
            if arbitraje.error:
                logger.warning(f"[cabecera] LLM árbitro error en '{campo}': {arbitraje.error}")
            return resolution

        # El LLM eligió un candidato → REVALIDAR antes de promover a valor final
        candidato_elegido = candidatos[arbitraje.indice_elegido]
        valor_propuesto = candidato_elegido.get("valor_normalizado")

        if not valor_propuesto:
            logger.warning(f"[cabecera] LLM eligió candidato sin valor para '{campo}'")
            return resolution

        # Revalidar según el tipo de campo
        revalidacion_ok, motivo_reval = _revalidar_valor(campo, valor_propuesto)

        if not revalidacion_ok:
            # Corrección LLM no pasa revalidación → ignorar, mantener decisión anterior
            resolution.motivo += (
                f" | LLM propuso '{valor_propuesto}' pero FALLA revalidación: {motivo_reval}"
            )
            logger.warning(
                f"[cabecera] LLM árbitro propuso '{valor_propuesto}' para '{campo}' "
                f"pero falla revalidación: {motivo_reval}"
            )
            return resolution

        # Revalidación OK → actualizar resolución con valor del LLM
        # El nivel nunca puede ser AUTO cuando el LLM intervino (siempre mínimo WARN)
        nueva_confianza = min(arbitraje.confianza, 0.89)  # Cap: no llega a auto_threshold si LLM intervino
        nueva_decision = (
            DecisionCampo.WARN
            if nueva_confianza >= self.umbral_warn
            else DecisionCampo.BLOCK
        )

        resolution.valor_final = valor_propuesto
        resolution.fuente_final = FuenteCandidato.LLM_ARBITRO
        resolution.confianza_final = nueva_confianza
        resolution.decision = nueva_decision
        resolution.motivo = (
            f"LLM árbitro eligió candidato {arbitraje.indice_elegido} "
            f"(conf_llm={arbitraje.confianza:.2f}): {arbitraje.justificacion}"
        )
        # Añadir candidato LLM a la lista de candidatos para trazabilidad
        resolution.candidatos.append({
            "valor_raw": valor_propuesto,
            "valor_normalizado": valor_propuesto,
            "fuente": FuenteCandidato.LLM_ARBITRO.value,
            "confianza": arbitraje.confianza,
            "motivo": f"Árbitro LLM: {arbitraje.justificacion}",
            "page_ref": candidato_elegido.get("page_ref"),
            "bbox_normalizado": candidato_elegido.get("bbox_normalizado"),
            "text_anchor_offsets": None,
            "validacion_ok": True,
            "motivo_validacion": motivo_reval,
        })

        logger.info(
            f"[cabecera] LLM árbitro actualiza '{campo}': "
            f"{nueva_decision.value} conf={nueva_confianza:.2f}"
        )
        return resolution

    def resolver(
        self,
        raw_document_ai: dict,
        documento_id: Optional[str] = None,
        documento_extraido: Optional[dict] = None,
        libro: Optional[str] = None,
    ) -> CabeceraResult:
        """
        Resolver todos los campos de cabecera desde el raw JSON de Document AI.

        Args:
            raw_document_ai: Dict del response de Document AI (REST o proto-dict)
            documento_id: ID del documento (si None, se genera UUID)

        Returns:
            CabeceraResult con un FieldResolution por campo y trazabilidad completa
        """
        doc_id = documento_id or str(uuid.uuid4())
        extractor = DocumentAIEntityExtractor(raw_document_ai)
        self._total_tokens_llm = 0

        logger.info(f"[cabecera] Iniciando resolución para documento_id={doc_id}")

        # ── Candidatos de Fase 2 OCR ─────────────────────────────────────
        candidatos_f2 = _candidatos_desde_fase2(documento_extraido or {})
        logger.info(
            f"[cabecera] Candidatos fase2_ocr disponibles: "
            f"nif={candidatos_f2['nif_entidad'] is not None} "
            f"nombre={candidatos_f2['nombre_entidad'] is not None} "
            f"num={candidatos_f2['numero_factura'] is not None} "
            f"fecha={candidatos_f2['fecha_expedicion'] is not None} "
            f"nif_rec={candidatos_f2['nif_receptor'] is not None} "
            f"nombre_rec={candidatos_f2['nombre_receptor'] is not None}"
        )

        # ── Resolver cada campo ──────────────────────────────────────────

        # 1. NIF (primero porque lo usa nombre para coherencia espacial)
        res_nif = resolver_nif_entidad(
            extractor, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_nif = self._aplicar_llm_si_necesario(res_nif, extractor, raw_document_ai)

        # 2. Nombre (recibe resolución de NIF para coherencia de página)
        res_nombre = resolver_nombre_entidad(
            extractor, res_nif, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_nombre = self._aplicar_llm_si_necesario(res_nombre, extractor, raw_document_ai)

        # 3. Número de factura
        res_num = resolver_numero_factura(
            extractor, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_num = self._aplicar_llm_si_necesario(res_num, extractor, raw_document_ai)

        # 4. Fechas
        res_fecha_exp = resolver_fecha_expedicion(
            extractor, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_fecha_exp = self._aplicar_llm_si_necesario(res_fecha_exp, extractor, raw_document_ai)

        res_fecha_oper = resolver_fecha_operacion(res_fecha_exp)  # Derivada de la expedición (F3)

        # 5. NIF receptor
        res_nif_receptor = resolver_nif_receptor(
            extractor, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_nif_receptor = self._aplicar_llm_si_necesario(res_nif_receptor, extractor, raw_document_ai)

        # 6. Nombre receptor (recibe NIF receptor para coherencia espacial)
        res_nombre_receptor = resolver_nombre_receptor(
            extractor, res_nif_receptor, self.umbral_auto, self.umbral_warn,
            candidatos_fase2=candidatos_f2,
        )
        res_nombre_receptor = self._aplicar_llm_si_necesario(res_nombre_receptor, extractor, raw_document_ai)

        # ── Autofactura: invertir emisor/receptor si procede ─────────────
        # En "facturación por el destinatario" la operadora se imprime como
        # emisor, pero el emisor legal es el titular del local. Hay dos
        # disparadores: (a) marcador textual en el OCR, o (b) que el NIF en rol
        # de cliente para el libro sea una operadora conocida (regla determinista
        # que cubre operadoras como Luckia sin marcador). En ambos casos solo se
        # invierte si ambos NIF están resueltos (gating: nunca fabricar el titular).
        trigger_marcador = _es_autofactura(extractor.texto_completo(), self._autofactura_marcadores)
        trigger_operadora = _operadora_en_rol_cliente(
            res_nif.valor_final, res_nif_receptor.valor_final, libro, self._operadora_nifs
        )
        if (self._autofactura_enabled
                and (trigger_marcador or trigger_operadora)
                and res_nif.valor_final and res_nif_receptor.valor_final):
            motivo = _AUTOFACTURA_MOTIVO if trigger_marcador else _OPERADORA_MOTIVO
            _swap_identidad_valores(res_nif, res_nif_receptor, motivo=motivo)
            _swap_identidad_valores(res_nombre, res_nombre_receptor, motivo=motivo)
            logger.info(
                "[cabecera] AUTOFACTURA detectada (%s) — emisor/receptor invertidos "
                "(nif_entidad=%s, nif_receptor=%s)",
                "marcador" if trigger_marcador else "operadora",
                res_nif.valor_final, res_nif_receptor.valor_final,
            )

        # ── Recoger motivos de revisión de campos obligatorios ───────────
        motivos_revision = []
        for res in [res_nif, res_nombre, res_num, res_fecha_exp, res_nif_receptor, res_nombre_receptor]:
            if res.decision != DecisionCampo.AUTO:
                motivos_revision.append(f"{res.campo}: {res.motivo}")

        # ── Construir resultado ──────────────────────────────────────────
        resultado = CabeceraResult(
            documento_id=doc_id,
            nif_entidad=res_nif.to_dict(),
            nombre_entidad=res_nombre.to_dict(),
            numero_factura=res_num.to_dict(),
            fecha_expedicion=res_fecha_exp.to_dict(),
            fecha_operacion=res_fecha_oper.to_dict(),
            nif_receptor=res_nif_receptor.to_dict(),
            nombre_receptor=res_nombre_receptor.to_dict(),
            motivos_revision=motivos_revision,
            llm_usado=self._total_tokens_llm > 0,
            tokens_llm=self._total_tokens_llm,
        ).finalizar()

        logger.info(
            f"[cabecera] Resolución completada: "
            f"decision_global={resultado.decision_global} "
            f"llm_usado={resultado.llm_usado} tokens={resultado.tokens_llm}"
        )
        return resultado


# ──────────────────────────────────────────────────────────
# Utilidades internas
# ──────────────────────────────────────────────────────────

_AUTOFACTURA_MOTIVO = (
    "AUTOFACTURA (facturación por el destinatario): emisor/receptor "
    "invertidos automáticamente — verificar."
)

_OPERADORA_MOTIVO = (
    "AUTOFACTURA (operadora de máquinas recreativas en rol de cliente): "
    "emisor/receptor invertidos automáticamente — la operadora no puede ser "
    "el titular; verificar."
)


def _normalizar_marcador(texto: str) -> str:
    """Normalizar para comparación robusta: sin acentos, mayúsculas, espacios colapsados."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", texto or "")
        if not unicodedata.combining(c)
    )
    return " ".join(sin_acentos.upper().split())


def _es_autofactura(texto_ocr: str, marcadores: list[str]) -> bool:
    """True si el OCR contiene alguno de los marcadores de autofactura (sin acentos)."""
    if not texto_ocr or not marcadores:
        return False
    texto_norm = _normalizar_marcador(texto_ocr)
    return any(
        _normalizar_marcador(m) in texto_norm
        for m in marcadores if m and m.strip()
    )


# Campo de identidad que representa al CLIENTE de la gestoría según el libro.
# Espejo de LIBRO_A_ROL_CLIENTE (phase4_customer/resolver.py); se admite forma
# larga ("21_VENTAS_INGRESOS") y corta ("ventas"/"ingresos") por robustez.
_LIBRO_A_CAMPO_CLIENTE = {
    "20_COMPRAS_GASTOS":  "nif_receptor",
    "21_VENTAS_INGRESOS": "nif_entidad",
    "22_BIENES_INVERSION": "nif_receptor",
    "compras":  "nif_receptor",
    "gastos":   "nif_receptor",
    "ventas":   "nif_entidad",
    "ingresos": "nif_entidad",
    "bienes":   "nif_receptor",
}


def _operadora_en_rol_cliente(
    nif_entidad: Optional[str],
    nif_receptor: Optional[str],
    libro: Optional[str],
    operadora_nifs: set[str],
) -> bool:
    """True si el NIF que ocupa el rol de CLIENTE para este libro es una operadora.

    En ventas el cliente es el emisor (``nif_entidad``); en compras/bienes es el
    receptor. Si ese NIF es una operadora conocida, los roles están invertidos
    (la operadora nunca es el titular) y procede el swap. Si ``libro`` es None o
    desconocido, devuelve False (no se puede determinar el rol con seguridad), de
    modo que las compras legítimas donde la operadora es el proveedor no se tocan.
    """
    if not operadora_nifs or not libro:
        return False
    campo = _LIBRO_A_CAMPO_CLIENTE.get(libro)
    if campo is None:
        return False
    valor = nif_entidad if campo == "nif_entidad" else nif_receptor
    return (valor or "").strip().upper() in operadora_nifs


def _swap_identidad_valores(
    res_a: FieldResolution,
    res_b: FieldResolution,
    motivo: str = _AUTOFACTURA_MOTIVO,
) -> None:
    """
    Intercambiar in-place el "paquete de valor" entre dos resoluciones de identidad,
    conservando ``campo``. Tras el swap, fuerza decisión >= WARN y antepone ``motivo``
    para señalar la inversión heurística al revisor humano.
    """
    for attr in ("valor_final", "fuente_final", "confianza_final", "decision",
                 "candidatos", "page_ref", "bbox_normalizado", "validaciones"):
        a_val = getattr(res_a, attr)
        setattr(res_a, attr, getattr(res_b, attr))
        setattr(res_b, attr, a_val)

    for res in (res_a, res_b):
        if res.decision == DecisionCampo.AUTO:
            res.decision = DecisionCampo.WARN
        res.motivo = f"{motivo} | {res.motivo}"


def _extraer_crop_region(
    raw_document_ai: dict,
    bbox: dict,
    page_ref: int
) -> Optional[bytes]:
    """
    Extraer recorte visual de región del documento desde raw JSON de Document AI.

    Args:
        raw_document_ai: Dict completo del response de Document AI
        bbox: Dict con {x_min, y_min, x_max, y_max} normalizado [0,1]
        page_ref: Número de página (0-indexed)

    Returns:
        bytes del PNG recortado, o None si no hay imagen disponible
    """
    try:
        import base64
        from io import BytesIO
        from PIL import Image

        doc = raw_document_ai.get("document", raw_document_ai)
        pages = doc.get("pages", [])

        if page_ref >= len(pages):
            logger.warning(f"[crop] page_ref {page_ref} fuera de rango (total pages: {len(pages)})")
            return None

        page = pages[page_ref]
        img_content_b64 = page.get("image", {}).get("content")

        if not img_content_b64:
            logger.info("[crop] No hay image.content en página — fallback a solo texto")
            return None

        # Decodificar base64
        img_bytes = base64.b64decode(img_content_b64)
        img = Image.open(BytesIO(img_bytes))
        width, height = img.size

        # Convertir bbox normalizado a píxeles
        x_min_px = int(bbox["x_min"] * width)
        y_min_px = int(bbox["y_min"] * height)
        x_max_px = int(bbox["x_max"] * width)
        y_max_px = int(bbox["y_max"] * height)

        if x_min_px >= x_max_px or y_min_px >= y_max_px:
            logger.warning(f"[crop] bbox inválido: {bbox}")
            return None

        # Añadir margen de contexto (10% en cada dirección)
        margin_x = int((x_max_px - x_min_px) * 0.1)
        margin_y = int((y_max_px - y_min_px) * 0.1)

        x_min_px = max(0, x_min_px - margin_x)
        y_min_px = max(0, y_min_px - margin_y)
        x_max_px = min(width, x_max_px + margin_x)
        y_max_px = min(height, y_max_px + margin_y)

        # Recortar y convertir a PNG bytes
        cropped = img.crop((x_min_px, y_min_px, x_max_px, y_max_px))
        buffer = BytesIO()
        cropped.save(buffer, format="PNG")
        crop_bytes = buffer.getvalue()

        logger.info(
            f"[crop] Región extraída: página={page_ref} bbox={bbox} "
            f"tamaño_crop={cropped.size} bytes={len(crop_bytes)}"
        )
        return crop_bytes

    except ImportError:
        logger.warning("[crop] PIL no disponible — fallback a solo texto")
        return None
    except Exception as e:
        logger.warning(f"[crop] Error al extraer región: {e}")
        return None


def _necesita_llm_con_umbral(resolution: FieldResolution, umbral_llm: float) -> bool:
    """True si la resolución tiene confianza baja o conflicto entre candidatos."""
    if resolution.decision == DecisionCampo.BLOCK:
        return True  # BLOCK siempre merece intento de LLM
    if resolution.confianza_final < umbral_llm:
        return True
    # Conflicto: múltiples candidatos viables con valores distintos
    viables = [
        c for c in resolution.candidatos
        if c.get("validacion_ok") is not False
        and c.get("valor_normalizado")
    ]
    valores_distintos = {c["valor_normalizado"] for c in viables}
    return len(valores_distintos) > 1


def _revalidar_valor(campo: str, valor: str) -> tuple[bool, str]:
    """
    Revalidar el valor elegido por el LLM según el tipo de campo.

    Returns:
        (es_valido: bool, motivo: str)
    """
    if not valor:
        return False, "Valor vacío"

    if campo in ("nif_entidad", "nif_receptor"):
        v = validar_identificador_fiscal(valor)
        return v.es_valido, v.razon

    if campo == "fecha_expedicion":
        import re as _re
        # Verificar formato ISO 8601
        if _re.match(r'^\d{4}-\d{2}-\d{2}$', valor):
            try:
                from datetime import date
                parts = valor.split("-")
                date(int(parts[0]), int(parts[1]), int(parts[2]))
                return True, "Fecha ISO 8601 válida"
            except ValueError as e:
                return False, f"Fecha ISO inválida: {e}"
        return False, f"Formato de fecha no es ISO 8601: '{valor}'"

    if campo == "numero_factura":
        if len(valor.strip()) >= 2:
            return True, "Número de factura con longitud mínima"
        return False, "Número de factura demasiado corto"

    if campo in ("nombre_entidad", "nombre_receptor"):
        if len(valor.strip()) >= 2:
            return True, f"Nombre de {'receptor' if campo == 'nombre_receptor' else 'entidad'} con longitud mínima"
        return False, f"Nombre de {'receptor' if campo == 'nombre_receptor' else 'entidad'} demasiado corto"

    # Campo desconocido: aceptar con aviso
    return True, f"Sin validación específica para campo '{campo}'"


def _extraer_contexto_relevante(
    texto_ocr: str,
    campo: str,
    candidatos: list[dict] = None,
    max_chars: int = 300,
) -> str:
    """
    Extraer un fragmento del texto OCR relevante para el campo dado.

    Busca TODAS las ocurrencias de keywords y elige la más cercana a los
    candidatos conocidos (usando text_anchor_offsets). Esto evita enviar
    al LLM contexto irrelevante (ej: pie de página en lugar de cabecera).

    Args:
        texto_ocr: Texto completo del documento OCR
        campo: Campo para el que se busca contexto
        candidatos: Lista de candidatos con text_anchor_offsets (opcional)
        max_chars: Máximo de caracteres a extraer

    Returns:
        Fragmento de texto OCR más relevante para el campo
    """
    if not texto_ocr:
        return ""

    _contextos_campo = {
        "nif_entidad": ["cif", "nif", "nie", "tax id", "vat"],
        "nombre_entidad": ["razón social", "empresa", "entidad", "supplier", "from"],
        "numero_factura": ["factura", "invoice", "fra", "nº", "número"],
        "fecha_expedicion": ["fecha", "date", "expedición", "emisión"],
        "nif_receptor": ["cliente", "destinatario", "receptor", "bill to", "customer"],
        "nombre_receptor": ["cliente", "destinatario", "receptor", "bill to", "customer"],
    }

    keywords = _contextos_campo.get(campo, [])
    texto_lower = texto_ocr.lower()

    # Extraer offsets de candidatos si existen
    candidato_offsets = []
    if candidatos:
        for c in candidatos:
            offsets = c.get("text_anchor_offsets")
            if offsets:
                for off in offsets:
                    start_idx = off.get("startIndex", 0)
                    candidato_offsets.append(start_idx)

    # Buscar TODAS las ocurrencias de keywords
    mejor_match = None
    mejor_distancia = float("inf")

    for kw in keywords:
        idx = 0
        while True:
            idx = texto_lower.find(kw, idx)
            if idx < 0:
                break

            # Si hay offsets de candidatos, calcular distancia
            if candidato_offsets:
                distancia = min(abs(idx - off) for off in candidato_offsets)
                if distancia < mejor_distancia:
                    mejor_distancia = distancia
                    mejor_match = idx
            else:
                # Sin offsets: preferir ocurrencias en primera mitad del doc (cabecera)
                if idx < len(texto_ocr) // 2:
                    if mejor_match is None or idx < mejor_match:
                        mejor_match = idx

            idx += 1

    if mejor_match is not None:
        start = max(0, mejor_match - 20)
        end = min(len(texto_ocr), mejor_match + max_chars)
        return texto_ocr[start:end]

    # Sin contexto léxico → devolver inicio del documento
    return texto_ocr[:max_chars]
