"""
Estructuración de texto OCR a documento_extraido.json vía Gemini.

Recibe texto plano extraído por Cloud Vision y lo envía a Gemini 2.5 Flash
para obtener un JSON estructurado con los campos fiscales de la factura.
Asigna confianzas deterministas y construye el esquema de salida.

Reemplaza el antiguo mapper de Document AI entities.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from google.oauth2 import service_account

from .config import settings as get_settings

logger = logging.getLogger("pipeline.ocr")

# ── Constantes de confianza ──────────────────────────────────────────────
CONFIANZA_CAMPO_OK = 0.95
CONFIANZA_CAMPO_KO = 0.0
CONFIANZA_TOTAL_WARN = 0.5


def _init_gemini_model() -> genai.Client:
    """Inicializar cliente Gemini (google-genai SDK) para estructuración OCR."""
    cfg = get_settings()

    credentials = None
    creds_path = cfg.google_application_credentials
    if creds_path:
        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

    client = genai.Client(
        vertexai=True,
        project=cfg.google_cloud_project_id,
        location=cfg.gemini_ocr_location,
        credentials=credentials,
        # timeout en ms (HttpOptions). Evita que un cuelgue de Gemini bloquee el lote.
        http_options=types.HttpOptions(
            api_version="v1",
            timeout=cfg.gemini_ocr_timeout_seconds * 1000,
        ),
    )
    logger.info(
        f"[Gemini] Cliente Vertex AI inicializado: "
        f"project={cfg.google_cloud_project_id} location={cfg.gemini_ocr_location} "
        f"model={cfg.gemini_ocr_model} api=v1"
    )
    return client


def construir_prompt_gemini(texto_plano: str) -> str:
    """Construir prompt para Gemini con el texto OCR de la factura."""
    return f"""Eres un sistema de extracción de datos fiscales para facturas españolas.

A continuación recibes el texto extraído por OCR de una factura.
Tu única tarea es devolver un JSON con los datos estructurados.
No añadas explicaciones, comentarios ni texto fuera del JSON.

CAMPOS A EXTRAER:
- numero_factura: string (ej: "F-2026-001", "A/123", "2026/0045")
- fecha_expedicion: string en formato YYYY-MM-DD (fecha de la factura, NO la fecha actual)
- nombre_entidad: string (razón social del emisor/proveedor)
- nif_entidad: string (NIF/CIF del emisor, ej: "B12345678", "A87654321")
- nombre_receptor: string (razón social del receptor/cliente)
- nif_receptor: string (NIF/CIF del receptor)
- total_factura: número decimal (importe total con IVA incluido)
- lineas_fiscales: array con TODOS los tramos de IVA presentes en la factura

Cada elemento de lineas_fiscales debe tener:
- base_euros: número decimal (base imponible del tramo)
- tipo_porcentaje: número decimal.
  Usa 0.0 para operaciones al tipo 0% (exportaciones, intracomunitarias).
  Usa null si la línea está exenta de IVA o no sujeta (ej: "exento art. 20 LIVA").
  No uses null para indicar que no encontraste el tipo — si no hay tipo en
  la factura pero hay IVA, usa el tipo que aparezca.
  Tipos habituales en España: 4.0, 10.0, 21.0
- cuota: número decimal (importe del IVA = base * tipo / 100)
- total_linea: número decimal (base + cuota) — incluir si aparece explícitamente

IMPORTANTE — múltiples tramos de IVA:
Si la factura tiene productos al 21% y otros al 10%, devuelve DOS entradas en
lineas_fiscales, una por cada tramo.

EJEMPLO de salida esperada para factura con dos tramos de IVA:
{{
  "numero_factura": "F-2026-001",
  "fecha_expedicion": "2026-03-15",
  "nombre_entidad": "Bebidas García S.L.",
  "nif_entidad": "B12345678",
  "nombre_receptor": "Restaurante Pepe S.L.",
  "nif_receptor": "B20091754",
  "total_factura": 159.04,
  "lineas_fiscales": [
    {{
      "base_euros": 24.00,
      "tipo_porcentaje": 21.0,
      "cuota": 5.04,
      "total_linea": 29.04
    }},
    {{
      "base_euros": 125.00,
      "tipo_porcentaje": 4.0,
      "cuota": 5.00,
      "total_linea": 130.00
    }}
  ]
}}

Si un campo no existe en la factura, devuelve null para ese campo.
Si no hay líneas fiscales distinguibles, devuelve lineas_fiscales como array vacío [].

TEXTO DE LA FACTURA:
{texto_plano}

Devuelve ÚNICAMENTE el JSON, sin texto adicional."""


def estructurar_factura(texto_plano: str, client: genai.Client) -> Optional[dict]:
    """
    Enviar texto OCR a Gemini y obtener JSON estructurado.

    Reintenta una vez en caso de fallo. Devuelve None si falla persistentemente.
    """
    cfg = get_settings()
    model_name = cfg.gemini_ocr_model
    prompt = construir_prompt_gemini(texto_plano)

    generation_config = types.GenerateContentConfig(
        temperature=0.05,
        max_output_tokens=2048,
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    max_retries = 3
    for intento in range(max_retries):
        try:
            logger.info(f"[Gemini] Llamada {intento + 1}/{max_retries} a {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=generation_config,
            )
            raw = response.text.strip()

            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            result = json.loads(raw)

            num_campos = sum(1 for k, v in result.items() if v is not None and k != "lineas_fiscales")
            num_lineas = len(result.get("lineas_fiscales") or [])
            logger.info(f"[Gemini] JSON recibido — {num_campos} campos, {num_lineas} líneas fiscales")

            return result

        except Exception as e:
            is_quota = "429" in str(e) or "quota" in str(e).lower() or "rate" in str(e).lower()
            if intento == max_retries - 1:
                logger.error(f"[Gemini] Fallo persistente tras {max_retries} intentos: {e}", exc_info=True)
                return None
            backoff = 2 ** (intento + 1)  # 2s, 4s
            if is_quota:
                backoff = backoff * 5  # 10s, 20s para errores de quota
                logger.warning(f"[Gemini] Quota/rate limit detectado, backoff={backoff}s: {e}")
            else:
                logger.warning(f"[Gemini] Reintento {intento + 1} tras error, backoff={backoff}s: {e}")
            time.sleep(backoff)

    return None


def validar_suma_fiscal(lineas_fiscales: List[dict], total_factura: Optional[float]) -> bool:
    """
    Verificar que suma(base + cuota) ≈ total_factura con tolerancia 0.02€.

    Si no hay datos suficientes para verificar, devuelve True (no penalizar).
    """
    if not lineas_fiscales or total_factura is None:
        return True

    suma = sum(
        l.get("base_euros", 0) + l.get("cuota", 0)
        for l in lineas_fiscales
        if l.get("base_euros") is not None and l.get("cuota") is not None
    )

    if suma == 0:
        return True

    ok = abs(suma - total_factura) <= 0.02
    if not ok:
        logger.warning(
            "[Gemini] Suma fiscal NO cuadra: diferencia detectada → requiere_revision=True"
        )
    return ok


def _confianza_campo(valor: Any) -> float:
    """Asignar confianza según presencia y validez del valor."""
    if valor is None or valor == "":
        return CONFIANZA_CAMPO_KO
    if isinstance(valor, str) and not valor.strip():
        return CONFIANZA_CAMPO_KO
    if isinstance(valor, float) and valor != valor:  # NaN check
        return CONFIANZA_CAMPO_KO
    return CONFIANZA_CAMPO_OK


def construir_documento_extraido(
    gemini_data: dict,
    archivo: str,
    paginas: int,
    carpeta_entrada: str
) -> Dict[str, Any]:
    """
    Construir documento_extraido.json desde datos de Gemini.

    Produce exactamente el mismo esquema que el antiguo mapper de Document AI
    para mantener compatibilidad con el resto del pipeline.
    """
    suma_ok = validar_suma_fiscal(
        gemini_data.get("lineas_fiscales", []),
        gemini_data.get("total_factura")
    )
    confianza_total = CONFIANZA_CAMPO_OK if suma_ok else CONFIANZA_TOTAL_WARN

    lineas_fiscales_out = []
    for linea in gemini_data.get("lineas_fiscales") or []:
        base = linea.get("base_euros")
        tipo = linea.get("tipo_porcentaje")
        cuota = linea.get("cuota")
        total_linea = linea.get("total_linea")
        conf_linea = CONFIANZA_CAMPO_OK if (base is not None and tipo is not None) else CONFIANZA_CAMPO_KO

        lineas_fiscales_out.append({
            "base_euros":      {"valor": base,       "confianza": conf_linea},
            "tipo_porcentaje": {"valor": tipo,        "confianza": conf_linea},
            "cuota":           {"valor": cuota,       "confianza": conf_linea if cuota is not None else CONFIANZA_CAMPO_KO},
            "total_linea":     {"valor": total_linea, "confianza": conf_linea if total_linea is not None else CONFIANZA_CAMPO_KO},
        })

    # F3: la fecha de operación se deriva de la expedición (no se fabrica con hoy@1.0).
    fecha_exp_valor = gemini_data.get("fecha_expedicion")

    return {
        "documento_id": str(uuid.uuid4()),
        "origen": {
            "archivo":         archivo,
            "pagina_count":    paginas,
            "ocr_engine":      "cloud_vision+gemini-2.5-flash",
            "carpeta_entrada": carpeta_entrada
        },
        "cliente_destino": {
            "nif_receptor":    {"valor": gemini_data.get("nif_receptor"),    "confianza": _confianza_campo(gemini_data.get("nif_receptor"))},
            "nombre_receptor": {"valor": gemini_data.get("nombre_receptor"), "confianza": _confianza_campo(gemini_data.get("nombre_receptor"))},
        },
        "identificacion": {
            "fecha_operacion":  {"valor": fecha_exp_valor, "confianza": _confianza_campo(fecha_exp_valor)},
            "fecha_expedicion": {"valor": fecha_exp_valor, "confianza": _confianza_campo(fecha_exp_valor)},
            "numero_factura":   {"valor": gemini_data.get("numero_factura"),   "confianza": _confianza_campo(gemini_data.get("numero_factura"))},
            "nombre_entidad":   {"valor": gemini_data.get("nombre_entidad"),   "confianza": _confianza_campo(gemini_data.get("nombre_entidad"))},
            "nif_entidad":      {"valor": gemini_data.get("nif_entidad"),      "confianza": _confianza_campo(gemini_data.get("nif_entidad"))},
        },
        "fiscal": {
            "total_euros":       {"valor": gemini_data.get("total_factura"), "confianza": confianza_total},
            "requiere_revision": not suma_ok,
            "lineas_fiscales":   lineas_fiscales_out,
        },
        "semantica": {
            "lineas_semanticas": [
                {
                    "concepto_raw":         {"valor": None, "confianza": 0.0},
                    "concepto_normalizado":  None,
                    "cuenta_contable":       {"valor": None, "confianza": None}
                }
            ]
        }
    }


def json_minimos(archivo: str, carpeta_entrada: str, texto_plano: str = "") -> Dict[str, Any]:
    """
    JSON mínimo de fallback cuando Gemini no responde.

    Permite al ensamblador registrar la incidencia con BLOCK controlado.
    Intenta rescatar número de factura por regex del texto OCR.
    """
    numero_regex = None
    if texto_plano:
        match = re.search(r'\b[A-Z]{0,3}[-/]?\d{4}[-/]\d{2,6}\b', texto_plano)
        if match:
            numero_regex = match.group(0)

    return {
        "documento_id": str(uuid.uuid4()),
        "origen": {
            "archivo": archivo,
            "ocr_engine": "cloud_vision+gemini-2.5-flash",
            "carpeta_entrada": carpeta_entrada
        },
        "requiere_revision": True,
        "error_extraccion": "gemini_timeout_or_quota",
        "cliente_destino": {
            "nif_receptor":    {"valor": None, "confianza": 0.0},
            "nombre_receptor": {"valor": None, "confianza": 0.0},
        },
        "identificacion": {
            "fecha_operacion":  {"valor": None, "confianza": 0.0},
            "fecha_expedicion": {"valor": None, "confianza": 0.0},
            "numero_factura":   {"valor": numero_regex, "confianza": 0.5 if numero_regex else 0.0},
            "nombre_entidad":   {"valor": None, "confianza": 0.0},
            "nif_entidad":      {"valor": None, "confianza": 0.0},
        },
        "fiscal": {
            "total_euros":     {"valor": None, "confianza": 0.0},
            "lineas_fiscales": [],
        },
        "semantica": {"lineas_semanticas": []}
    }
