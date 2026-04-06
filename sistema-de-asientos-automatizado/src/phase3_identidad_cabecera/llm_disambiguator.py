"""
LLM árbitro de desambiguación de candidatos.

Gemini Flash se usa como árbitro LOCAL en tres casos únicos:
  1. Dos o más candidatos viables con valores distintos para el mismo campo
  2. Confianza del mejor candidato por debajo de umbral LLM
  3. NIF detectado pero normalized_value ausente y regex produce conflicto

Gemini NO:
  - Lee el PDF desde cero
  - Genera un valor que no aparece ya en la lista de candidatos
  - Promueve un valor final sin que pase por validación posterior

El output de Gemini es siempre un índice de candidato + justificación,
nunca un valor libre. Esto cierra el vector de invención de datos.

El llamador (CabeceraResolver) es responsable de:
  - Revalidar el candidato elegido por Gemini con el validador determinista
  - Marcar la fuente como LLM_ARBITRO en el FieldResolution final
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("pipeline.identidad")

try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False


@dataclass
class ArbitrajeResult:
    """Resultado del árbitro LLM para un campo."""
    indice_elegido: Optional[int]   # Índice en la lista de candidatos (None = no pudo decidir)
    valor_propuesto: Optional[str]  # Valor del candidato elegido
    justificacion: str
    confianza: float                # Auto-reportada por el LLM (0.0–1.0)
    tokens_prompt: int
    tokens_respuesta: int
    error: Optional[str] = None     # Si hubo error técnico


_SYSTEM_ARBITRO = """\
Eres un árbitro de extracción de datos en facturas españolas.
Se te dará una lista de candidatos para un campo específico de una factura.
Cada candidato ya fue extraído por Google Document AI o por regex; no tienes que leer la factura desde cero.
Tu única tarea es elegir el candidato más probable y justificar por qué.

REGLAS ABSOLUTAS:
1. Devuelve ÚNICAMENTE un objeto JSON válido. Sin texto adicional, sin markdown.
2. Solo puedes elegir un candidato de la lista. No puedes inventar un valor nuevo.
3. Si ningún candidato es fiable, devuelve indice_elegido: null.
4. La confianza es tu estimación interna de 0.0 a 1.0.

Formato de respuesta:
{
  "indice_elegido": <número entero 0-based o null>,
  "justificacion": "<razón concisa de por qué ese candidato>",
  "confianza": <float 0.0-1.0>
}
""".strip()


def _prompt_arbitraje(campo: str, candidatos: list[dict], contexto_ocr: str = "") -> str:
    """Construir prompt de arbitraje para un campo con sus candidatos."""
    lines = [
        f"Campo: {campo}",
        f"Número de candidatos: {len(candidatos)}",
        "",
    ]
    for i, c in enumerate(candidatos):
        lines.append(
            f"Candidato {i}: valor='{c.get('valor_normalizado')}' "
            f"fuente={c.get('fuente')} "
            f"confianza_ocr={c.get('confianza', 0):.2f} "
            f"validacion={'OK' if c.get('validacion_ok') else 'FALLA' if c.get('validacion_ok') is False else 'N/A'}"
        )

    if contexto_ocr:
        # Dar solo los primeros 300 caracteres de contexto relevante
        lines.append("")
        lines.append(f"Fragmento OCR contextual (primeros 300 chars):")
        lines.append(contexto_ocr[:300])

    lines.append("")
    lines.append("¿Qué candidato elegirías como valor final para este campo?")
    return "\n".join(lines)


class LLMDisambiguator:
    """
    Árbitro Gemini Flash para resolución de conflictos entre candidatos.

    Instanciar una vez por ejecución del pipeline.

    NOTA IMPORTANTE sobre el SDK google-genai con Vertex AI:
    Por defecto el SDK usa endpoints v1beta1, que causa 404 en modelos Gemini
    en regiones europeas. Se fuerza api_version='v1' usando types.HttpOptions
    según documentación oficial: https://googleapis.github.io/python-genai/#api-selection
    """

    def __init__(
        self,
        project: str,
        location: str,
        model_name: str,
        max_retries: int,
        credentials_path: Optional[str] = None,
    ):
        if not _GENAI_AVAILABLE:
            raise ImportError("pip install google-genai")

        credentials = None
        if credentials_path:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        # CRÍTICO: forzar api_version='v1' para usar endpoints estables de Vertex AI.
        # Sin esto, el SDK usa v1beta1 por defecto, lo que causa 404 en modelos Gemini
        # independientemente de la región configurada.
        # Ref: https://googleapis.github.io/python-genai/#api-selection
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=credentials,
            http_options=types.HttpOptions(api_version="v1"),
        )
        self._generation_config = types.GenerateContentConfig(
            temperature=0.05,
            max_output_tokens=256,
            response_mime_type="application/json",
            system_instruction=_SYSTEM_ARBITRO,
            # Gemini 2.5 Flash tiene thinking mode habilitado por defecto.
            # Para tareas simples de arbitraje JSON, el thinking no aporta valor
            # y causa que response.text sea None (el primer part es thinking, no texto).
            # Ref: https://ai.google.dev/gemini-api/docs/thinking
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        self._model_name = model_name
        self._max_retries = max_retries
        logger.info(
            f"[LLM árbitro] Cliente Vertex AI inicializado: "
            f"project={project} location={location} model={model_name} api=v1"
        )

    def arbitrar(
        self,
        campo: str,
        candidatos: list[dict],
        contexto_ocr: str = "",
        imagen_crop: Optional[bytes] = None,
    ) -> ArbitrajeResult:
        """
        Pedir a Gemini que elija entre los candidatos.

        Args:
            campo: Nombre del campo ("nif_entidad", "numero_factura", etc.)
            candidatos: Lista de candidatos serializados (dicts con valor_normalizado, fuente, etc.)
            contexto_ocr: Fragmento del texto OCR relevante para el campo
            imagen_crop: bytes de PNG recortado de región del mejor candidato (opcional)

        Returns:
            ArbitrajeResult con el índice elegido y justificación
        """
        if not candidatos:
            return ArbitrajeResult(
                indice_elegido=None, valor_propuesto=None,
                justificacion="Sin candidatos para arbitrar",
                confianza=0.0, tokens_prompt=0, tokens_respuesta=0,
            )

        prompt = _prompt_arbitraje(campo, candidatos, contexto_ocr)
        ultimo_error = None

        # Preparar contenido multimodal si hay imagen
        contents = [prompt]
        if imagen_crop:
            try:
                image_part = types.Part.from_bytes(
                    data=imagen_crop,
                    mime_type="image/png",
                )
                contents = [prompt, image_part]
                logger.info(f"[LLM árbitro] Enviando crop visual ({len(imagen_crop)} bytes) para '{campo}'")
            except Exception as e:
                logger.warning(f"[LLM árbitro] Error al preparar imagen: {e} — usando solo texto")
                contents = [prompt]

        for intento in range(self._max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=contents,
                    config=self._generation_config,
                )

                # ── Extraer texto de la respuesta de forma robusta ──
                # response.text puede ser None si:
                #   - El modelo no generó texto (respuesta vacía / thinking-only)
                #   - La respuesta fue bloqueada por safety filters
                #   - finish_reason no es STOP (ej. MAX_TOKENS, SAFETY, etc.)
                raw = None
                try:
                    raw = response.text
                except (ValueError, AttributeError):
                    pass

                # Si response.text es None, intentar acceso directo a candidates
                if raw is None:
                    candidates = getattr(response, "candidates", None)
                    if candidates and len(candidates) > 0:
                        candidate = candidates[0]
                        # Diagnóstico: finish_reason
                        finish_reason = getattr(candidate, "finish_reason", "UNKNOWN")
                        safety_ratings = getattr(candidate, "safety_ratings", [])
                        content = getattr(candidate, "content", None)
                        parts = getattr(content, "parts", []) if content else []

                        # Intentar extraer texto de parts
                        for part in (parts or []):
                            part_text = getattr(part, "text", None)
                            if part_text:
                                raw = part_text
                                break

                        if raw is None:
                            logger.warning(
                                f"[LLM árbitro] Respuesta sin texto para '{campo}': "
                                f"finish_reason={finish_reason} "
                                f"n_parts={len(parts or [])} "
                                f"safety={safety_ratings}"
                            )
                            raise ValueError(
                                f"Respuesta vacía del modelo: finish_reason={finish_reason}"
                            )
                    else:
                        # Sin candidates en absoluto
                        prompt_feedback = getattr(response, "prompt_feedback", None)
                        logger.warning(
                            f"[LLM árbitro] Sin candidates en respuesta para '{campo}': "
                            f"prompt_feedback={prompt_feedback}"
                        )
                        raise ValueError("Respuesta sin candidates del modelo")

                # Limpiar fences defensivamente
                raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
                if not raw_clean:
                    raise ValueError("Texto de respuesta vacío tras limpieza")
                parsed = json.loads(raw_clean)

                idx = parsed.get("indice_elegido")
                justificacion = parsed.get("justificacion", "Sin justificación")
                confianza = max(0.0, min(1.0, float(parsed.get("confianza", 0.5))))

                usage = getattr(response, "usage_metadata", None)
                tp = getattr(usage, "prompt_token_count", 0) or 0
                tr = getattr(usage, "candidates_token_count", 0) or 0

                valor = None
                if idx is not None and 0 <= int(idx) < len(candidatos):
                    idx = int(idx)
                    valor = candidatos[idx].get("valor_normalizado")
                else:
                    idx = None  # Índice inválido → tratar como no pudo decidir

                logger.info(
                    f"[LLM árbitro] campo={campo} idx_elegido={idx} "
                    f"conf={confianza:.2f}"
                )
                return ArbitrajeResult(
                    indice_elegido=idx, valor_propuesto=valor,
                    justificacion=justificacion, confianza=confianza,
                    tokens_prompt=tp, tokens_respuesta=tr,
                )

            except Exception as e:
                ultimo_error = e
                logger.warning(f"[LLM árbitro] intento {intento+1} fallido para {campo}: {e}")
                if intento < self._max_retries:
                    time.sleep(2 ** intento)

        return ArbitrajeResult(
            indice_elegido=None, valor_propuesto=None,
            justificacion=f"Error técnico LLM: {ultimo_error}",
            confianza=0.0, tokens_prompt=0, tokens_respuesta=0,
            error=str(ultimo_error),
        )
