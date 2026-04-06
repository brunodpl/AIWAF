"""
Resolución semántica: concepto + cuenta contable.

Estrategia de resolución (prioridad descendente):
  1. Proveedor conocido (maestro_proveedores.yaml)
  2. Catálogo de patrones (catalogo_semantica.yaml)
  3. LLM especializado en PGC (Gemini) como clasificador semántico
  4. Validación: cuenta en whitelist, coherencia libro, bienes de inversión
  5. Si nada funciona → pendiente/warn para revisión humana

El LLM NO inventa cuentas: solo elige entre las del maestro contable.
Si human_review_required en el concepto → WARN mínimo.
Bienes de inversión → WARN mínimo (nunca AUTO).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .catalogo import (
    buscar_por_nif,
    buscar_por_patrones,
    cargar_catalogo_semantica,
    cargar_maestro_proveedores,
)

logger = logging.getLogger("pipeline.semantica")

# Defaults usados si config no los proporciona
_DEFAULT_UMBRAL_AUTO = 0.90
_DEFAULT_UMBRAL_WARN = 0.70

# Mapeo de libro → tipo de cuentas válidas en maestro_cuentas.yaml
_LIBRO_A_CUENTAS_KEY = {
    "20_COMPRAS_GASTOS":   "cuentas_validas_compras",
    "21_VENTAS_INGRESOS":  "cuentas_validas_ventas",
    "22_BIENES_INVERSION": "cuentas_validas_inmovilizado",
}

# Mapeo grupo PGC → libros esperados (para coherencia)
_GRUPO_A_LIBRO = {
    "6": "20_COMPRAS_GASTOS",
    "7": "21_VENTAS_INGRESOS",
    "2": "22_BIENES_INVERSION",
}


@dataclass
class CandidatoSemantico:
    concepto: Optional[str]
    cuentacontable: Optional[str]
    confianza: float
    fuente: str
    motivo: str


@dataclass
class ResolucionSemantica:
    """Resultado de la resolución para un campo (concepto o cuenta_contable)."""
    valor_final: Optional[str]
    fuente_final: str
    confianza_final: float
    decision: str  # auto, warn, block
    motivo: str
    candidatos: list[dict] = field(default_factory=list)


@dataclass
class ResultadoSemantico:
    concepto: ResolucionSemantica
    cuenta_contable: ResolucionSemantica
    decision_global: str
    requiere_revision_humana: bool
    motivos_revision: list[str]
    llm_usado: bool
    tokens_llm: int


# ── System prompt para LLM clasificador PGC ────────────────────

_SYSTEM_CLASIFICADOR_PGC = """\
Eres un clasificador contable experto en el Plan General Contable (PGC) español.
Tu tarea: dado el texto OCR de una factura y un catálogo de cuentas contables,
clasificar la operación eligiendo la cuenta y concepto más adecuados.

REGLAS ABSOLUTAS:
1. Devuelve ÚNICAMENTE un objeto JSON válido. Sin texto adicional, sin markdown.
2. Solo puedes elegir un concepto y cuenta de la lista de opciones proporcionada.
3. No puedes inventar cuentas ni conceptos que no estén en la lista.
4. Si ninguna opción encaja con claridad, devuelve concepto: null, cuenta: null.
5. La confianza es tu estimación interna de 0.0 a 1.0.

Formato de respuesta:
{
  "concepto": "<key del concepto elegido o null>",
  "cuenta_contable": "<código de cuenta elegido o null>",
  "justificacion": "<razón concisa>",
  "confianza": <float 0.0-1.0>
}
""".strip()


def _construir_prompt_clasificacion(
    texto_ocr: str,
    nombre_emisor: str,
    libro: str,
    opciones_catalogo: list[dict],
) -> str:
    """Construir prompt para clasificación semántica LLM."""
    lines = [
        f"Libro contable: {libro}",
        f"Emisor: {nombre_emisor}" if nombre_emisor else "",
        "",
        "Texto OCR de la factura (primeros 500 chars):",
        texto_ocr[:500],
        "",
        "Opciones disponibles del catálogo PGC:",
    ]
    for opt in opciones_catalogo:
        lines.append(
            f"  - concepto='{opt['concepto']}' cuenta='{opt['cuentacontable']}' "
            f"(patrones: {', '.join(opt.get('patrones_raw', [])[:3])})"
        )
    lines.append("")
    lines.append("¿Qué concepto y cuenta contable corresponden a esta factura?")
    return "\n".join(l for l in lines)


def _llamar_llm_clasificador(
    texto_ocr: str,
    nombre_emisor: str,
    libro: str,
    opciones_catalogo: list[dict],
    config: object,
) -> Optional[CandidatoSemantico]:
    """
    Llamar a Gemini para clasificación semántica.

    Returns:
        CandidatoSemantico o None si falla.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("[semantica] google-genai no disponible, skip LLM")
        return None

    try:
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_file(
            config.google_application_credentials,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        client = genai.Client(
            vertexai=True,
            project=config.google_cloud_project_id,
            location=config.gemini_arbitro_location,
            credentials=credentials,
            http_options=types.HttpOptions(api_version="v1"),
        )

        prompt = _construir_prompt_clasificacion(
            texto_ocr, nombre_emisor, libro, opciones_catalogo
        )

        generation_config = types.GenerateContentConfig(
            temperature=0.05,
            max_output_tokens=256,
            response_mime_type="application/json",
            system_instruction=_SYSTEM_CLASIFICADOR_PGC,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

        response = client.models.generate_content(
            model=config.gemini_arbitro_model,
            contents=prompt,
            config=generation_config,
        )

        if not response.text:
            logger.warning("[semantica] LLM devolvió respuesta vacía")
            return None

        data = json.loads(response.text)
        concepto = data.get("concepto")
        cuenta = data.get("cuenta_contable")
        confianza = float(data.get("confianza", 0.0))

        # Calcular tokens
        tokens = 0
        if response.usage_metadata:
            tokens = (
                getattr(response.usage_metadata, "prompt_token_count", 0)
                + getattr(response.usage_metadata, "candidates_token_count", 0)
            )

        if concepto is None and cuenta is None:
            logger.info("[semantica] LLM no pudo clasificar (null)")
            return None

        # Validar que concepto/cuenta existen en catálogo
        conceptos_validos = {o["concepto"] for o in opciones_catalogo}
        cuentas_validas = {o["cuentacontable"] for o in opciones_catalogo}

        if concepto and concepto not in conceptos_validos:
            logger.warning(
                f"[semantica] LLM propuso concepto '{concepto}' fuera del catálogo, descartando"
            )
            return None

        if cuenta and cuenta not in cuentas_validas:
            logger.warning(
                f"[semantica] LLM propuso cuenta '{cuenta}' fuera del catálogo, descartando"
            )
            return None

        # Cap confianza a 0.89 (nunca AUTO directo por LLM)
        confianza = min(confianza, 0.89)

        logger.info(
            f"[semantica] LLM clasificó: concepto={concepto} cuenta={cuenta} "
            f"confianza={confianza:.2f} tokens={tokens}"
        )

        return CandidatoSemantico(
            concepto=concepto,
            cuentacontable=cuenta,
            confianza=confianza,
            fuente="llm_pgc",
            motivo=data.get("justificacion", "Clasificación LLM PGC"),
        )

    except Exception as e:
        logger.error(f"[semantica] Error LLM: {e}", exc_info=True)
        return None


def _cargar_conceptos_con_review(maestro_path: str) -> dict[str, bool]:
    """
    Extraer human_review_required de maestro_contable_fiscal.yaml para cada concepto.

    Returns:
        Dict concepto_key → human_review_required
    """
    path = Path(maestro_path)
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        conceptos = data.get("enums", {}).get("concepto", {}).get("allowed_values", [])
        return {
            c["key"]: c.get("human_review_required", False)
            for c in conceptos
            if "key" in c
        }
    except Exception:
        return {}


def _cargar_cuentas_validas(maestro_cuentas_path: str) -> dict[str, set[str]]:
    """
    Cargar maestro_cuentas.yaml → whitelist de cuentas por tipo de libro.

    NOTA: El archivo usa un formato no-estándar con punto y coma como separador
    dentro de items YAML:  - "600"; "601"; "602"
    Esto NO es YAML válido, así que se parsea manualmente línea a línea.

    Returns:
        Dict tipo → set de códigos de cuenta válidos.
        Ej: {"cuentas_validas_compras": {"600", "601", ...}}
    """
    path = Path(maestro_cuentas_path)
    if not path.exists():
        return {}

    try:
        result: dict[str, set[str]] = {}
        current_key = None

        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                # Skip comments and empty lines
                if not stripped or stripped.startswith("#"):
                    continue
                # Key line: "cuentas_validas_compras:"
                if stripped.endswith(":") and not stripped.startswith("-"):
                    current_key = stripped[:-1].strip()
                    result[current_key] = set()
                # Value line: '  - "600"; "601"; "602"'
                elif stripped.startswith("-") and current_key is not None:
                    value_part = stripped[1:].strip()
                    for parte in value_part.split(";"):
                        cuenta = parte.strip().strip('"').strip("'").strip()
                        if cuenta:
                            result[current_key].add(cuenta)

        return result
    except Exception as e:
        logger.warning(f"[semantica] Error cargando maestro_cuentas: {e}")
        return {}


def _validar_cuenta_en_libro(
    cuenta: str | None,
    libro: str,
    cuentas_validas: dict[str, set[str]],
) -> tuple[bool, str]:
    """
    Validar que la cuenta propuesta está en la whitelist del libro.

    Returns:
        (es_valida, motivo)
    """
    if not cuenta:
        return False, "Cuenta contable vacía"

    cuentas_key = _LIBRO_A_CUENTAS_KEY.get(libro)
    if not cuentas_key:
        return True, f"Libro '{libro}' no tiene whitelist definida"

    whitelist = cuentas_validas.get(cuentas_key, set())
    if not whitelist:
        return True, f"Whitelist vacía para {cuentas_key}"

    if cuenta in whitelist:
        return True, f"Cuenta {cuenta} válida para libro {libro}"
    return False, f"Cuenta {cuenta} no está en whitelist de {libro}"


def _verificar_coherencia_libro(cuenta: str | None, libro: str) -> tuple[bool, str]:
    """
    Verificar que el grupo PGC de la cuenta es coherente con el libro.

    Grupo 6 = compras/gastos, Grupo 7 = ventas/ingresos, Grupo 2 = bienes inversión.
    """
    if not cuenta:
        return True, ""

    grupo = cuenta[0] if len(cuenta) >= 1 else ""
    libro_esperado = _GRUPO_A_LIBRO.get(grupo)

    if libro_esperado and libro_esperado != libro:
        return False, (
            f"Cuenta {cuenta} (grupo {grupo}) asignada a libro {libro}, "
            f"pero el grupo PGC {grupo} corresponde a {libro_esperado}"
        )
    return True, ""


def _es_bienes_inversion(libro: str) -> bool:
    """Detectar si el libro es bienes de inversión."""
    return "BIENES_INVERSION" in libro.upper()


# Prioridad de decisiones
_PRIORIDAD = {"block": 3, "warn": 2, "pendiente": 1, "auto": 0}


def resolver_semantica(
    texto_ocr: str,
    nif_emisor: Optional[str],
    nombre_emisor: Optional[str],
    libro: str,
    catalogo_path: str,
    proveedores_path: str,
    maestro_contable_path: str,
    maestro_cuentas_path: str = "data/maestros/maestro_cuentas.yaml",
    config: Optional[object] = None,
) -> ResultadoSemantico:
    """
    Resolver concepto y cuenta_contable para una factura.

    Args:
        texto_ocr: Texto completo OCR del documento
        nif_emisor: NIF del emisor (de fase 3.1 identidad)
        nombre_emisor: Nombre del emisor
        libro: Libro contable destino (e.g. "20_COMPRAS_GASTOS")
        catalogo_path: Ruta a catalogo_semantica.yaml
        proveedores_path: Ruta a maestro_proveedores.yaml
        maestro_contable_path: Ruta a maestro_contable_fiscal.yaml
        maestro_cuentas_path: Ruta a maestro_cuentas.yaml
        config: Settings del pipeline (para LLM). None = sin LLM.

    Returns:
        ResultadoSemantico con resolución de ambos campos
    """
    # 1. Cargar catálogos
    catalogo = cargar_catalogo_semantica(catalogo_path)
    proveedores = cargar_maestro_proveedores(proveedores_path)
    review_flags = _cargar_conceptos_con_review(maestro_contable_path)
    cuentas_validas = _cargar_cuentas_validas(maestro_cuentas_path)

    # Extraer umbrales de config (con defaults seguros)
    umbral_auto = getattr(config, "semantica_umbral_confianza_auto", None) or _DEFAULT_UMBRAL_AUTO
    umbral_warn = getattr(config, "semantica_umbral_confianza_warn", None) or _DEFAULT_UMBRAL_WARN
    umbral_catalogo = getattr(config, "semantica_umbral_catalogo", None) or _DEFAULT_UMBRAL_AUTO

    candidatos: list[CandidatoSemantico] = []
    llm_usado = False
    tokens_llm = 0

    # 2. Fuente 1: Proveedor conocido (máxima prioridad)
    resultado_proveedor = buscar_por_nif(nif_emisor or "", proveedores)
    if resultado_proveedor:
        cand = CandidatoSemantico(
            concepto=resultado_proveedor["concepto"],
            cuentacontable=resultado_proveedor["cuentacontable"],
            confianza=resultado_proveedor["confianza"],
            fuente="maestro_proveedores",
            motivo=f"Proveedor conocido NIF={nif_emisor}",
        )
        candidatos.append(cand)
        logger.info(
            f"[semantica] Proveedor conocido: {nif_emisor} → "
            f"concepto={cand.concepto} cuenta={cand.cuentacontable}"
        )

    # 3. Fuente 2: Patrones de catálogo
    matches_catalogo = buscar_por_patrones(texto_ocr, catalogo)
    for match in matches_catalogo:
        cand = CandidatoSemantico(
            concepto=match["concepto"],
            cuentacontable=match["cuentacontable"],
            confianza=match["confianza"],
            fuente="catalogo_semantica",
            motivo=f"Patrón '{match['patron_matched']}' encontrado en texto OCR",
        )
        candidatos.append(cand)

    # 4. Fuente 3: LLM clasificador PGC (si no hay candidatos de alta confianza)
    mejor_confianza = max(
        (c.confianza for c in candidatos), default=0.0
    )
    if mejor_confianza < umbral_auto and config is not None and catalogo:
        logger.info(
            f"[semantica] Mejor confianza={mejor_confianza:.2f} < {umbral_auto}, "
            f"invocando LLM clasificador PGC"
        )
        resultado_llm = _llamar_llm_clasificador(
            texto_ocr, nombre_emisor or "", libro, catalogo, config
        )
        if resultado_llm:
            llm_usado = True
            candidatos.append(resultado_llm)

    # 5. Seleccionar mejor candidato para concepto y cuenta
    concepto_res = _seleccionar_mejor(candidatos, "concepto", review_flags, umbral_auto, umbral_warn)
    cuenta_res = _seleccionar_mejor_cuenta(candidatos, concepto_res.valor_final, umbral_auto, umbral_warn)

    # 6. Validación: cuenta en whitelist del libro
    cuenta_valida, motivo_cuenta = _validar_cuenta_en_libro(
        cuenta_res.valor_final, libro, cuentas_validas
    )
    if not cuenta_valida and cuenta_res.valor_final is not None:
        logger.warning(f"[semantica] {motivo_cuenta}")
        cuenta_res.decision = "block"
        cuenta_res.motivo = motivo_cuenta

    # 7. Validación: coherencia grupo PGC con libro
    coherente, motivo_coherencia = _verificar_coherencia_libro(
        cuenta_res.valor_final, libro
    )
    if not coherente:
        logger.warning(f"[semantica] {motivo_coherencia}")
        if _PRIORIDAD.get(cuenta_res.decision, 0) < _PRIORIDAD["warn"]:
            cuenta_res.decision = "warn"
            cuenta_res.motivo = motivo_coherencia

    # 8. Bienes de inversión → WARN mínimo (nunca AUTO)
    if _es_bienes_inversion(libro):
        motivo_bi = "Bien de inversión requiere revisión humana obligatoria (normativa)"
        if _PRIORIDAD.get(concepto_res.decision, 0) < _PRIORIDAD["warn"]:
            concepto_res.decision = "warn"
            concepto_res.motivo += f" — {motivo_bi}"
        if _PRIORIDAD.get(cuenta_res.decision, 0) < _PRIORIDAD["warn"]:
            cuenta_res.decision = "warn"
            cuenta_res.motivo += f" — {motivo_bi}"
        logger.info(f"[semantica] Libro bienes inversión: forzando WARN mínimo")

    # 9. Decisión global
    p_concepto = _PRIORIDAD.get(concepto_res.decision, 0)
    p_cuenta = _PRIORIDAD.get(cuenta_res.decision, 0)
    decision_global = concepto_res.decision if p_concepto >= p_cuenta else cuenta_res.decision

    motivos = []
    if concepto_res.decision != "auto":
        motivos.append(f"concepto: {concepto_res.motivo}")
    if cuenta_res.decision != "auto":
        motivos.append(f"cuenta_contable: {cuenta_res.motivo}")

    requiere_revision = decision_global != "auto"

    return ResultadoSemantico(
        concepto=concepto_res,
        cuenta_contable=cuenta_res,
        decision_global=decision_global,
        requiere_revision_humana=requiere_revision,
        motivos_revision=motivos,
        llm_usado=llm_usado,
        tokens_llm=tokens_llm,
    )


def _seleccionar_mejor(
    candidatos: list[CandidatoSemantico],
    campo: str,
    review_flags: dict[str, bool],
    umbral_auto: float = _DEFAULT_UMBRAL_AUTO,
    umbral_warn: float = _DEFAULT_UMBRAL_WARN,
) -> ResolucionSemantica:
    """Seleccionar mejor candidato para concepto."""
    if not candidatos:
        return ResolucionSemantica(
            valor_final=None,
            fuente_final="pendiente",
            confianza_final=0.0,
            decision="pendiente",
            motivo=f"Sin candidatos para {campo}",
            candidatos=[],
        )

    # Ordenar por confianza descendente
    candidatos_sorted = sorted(candidatos, key=lambda c: c.confianza, reverse=True)
    mejor = candidatos_sorted[0]

    # Serializar candidatos para trazabilidad
    candidatos_serial = [
        {
            "valor": c.concepto,
            "cuentacontable": c.cuentacontable,
            "confianza": c.confianza,
            "fuente": c.fuente,
            "motivo": c.motivo,
        }
        for c in candidatos_sorted
    ]

    # Verificar human_review_required
    force_review = review_flags.get(mejor.concepto, False) if mejor.concepto else False

    # Determinar decisión
    if mejor.confianza >= umbral_auto and not force_review:
        decision = "auto"
        motivo = mejor.motivo
    elif mejor.confianza >= umbral_warn or force_review:
        decision = "warn"
        motivo = mejor.motivo
        if force_review:
            motivo += " (human_review_required por política interna)"
    else:
        decision = "warn"
        motivo = f"Confianza baja ({mejor.confianza:.2f}) para {campo}"

    return ResolucionSemantica(
        valor_final=mejor.concepto,
        fuente_final=mejor.fuente,
        confianza_final=mejor.confianza,
        decision=decision,
        motivo=motivo,
        candidatos=candidatos_serial,
    )


def _seleccionar_mejor_cuenta(
    candidatos: list[CandidatoSemantico],
    concepto_elegido: Optional[str],
    umbral_auto: float = _DEFAULT_UMBRAL_AUTO,
    umbral_warn: float = _DEFAULT_UMBRAL_WARN,
) -> ResolucionSemantica:
    """Seleccionar mejor candidato para cuenta_contable, priorizando coherencia con concepto."""
    if not candidatos:
        return ResolucionSemantica(
            valor_final=None,
            fuente_final="pendiente",
            confianza_final=0.0,
            decision="pendiente",
            motivo="Sin candidatos para cuenta_contable",
            candidatos=[],
        )

    # Priorizar candidato cuyo concepto coincida con el elegido
    coherentes = [c for c in candidatos if c.concepto == concepto_elegido]
    pool = coherentes if coherentes else candidatos

    pool_sorted = sorted(pool, key=lambda c: c.confianza, reverse=True)
    mejor = pool_sorted[0]

    candidatos_serial = [
        {
            "valor": c.cuentacontable,
            "concepto": c.concepto,
            "confianza": c.confianza,
            "fuente": c.fuente,
            "motivo": c.motivo,
        }
        for c in pool_sorted
    ]

    if mejor.confianza >= umbral_auto:
        decision = "auto"
    elif mejor.confianza >= umbral_warn:
        decision = "warn"
    else:
        decision = "warn"

    return ResolucionSemantica(
        valor_final=mejor.cuentacontable,
        fuente_final=mejor.fuente,
        confianza_final=mejor.confianza,
        decision=decision,
        motivo=mejor.motivo,
        candidatos=candidatos_serial,
    )
