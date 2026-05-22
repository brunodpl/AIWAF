"""
Resolucion semantica: concepto + cuenta contable.

Estrategia de resolucion (prioridad descendente):
  1. Proveedor conocido (maestro_proveedores.yaml)
  2. LLM clasificador PGC (Gemini) con lista limpia de cuentas
  3. Fallback a pendiente si LLM falla o no hay config
  4. Validacion: cuenta en whitelist, coherencia libro, bienes de inversion
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
    cargar_maestro_contable,
    cargar_maestro_proveedores,
    cuentas_para_libro,
)

logger = logging.getLogger("pipeline.semantica")

_DEFAULT_UMBRAL_AUTO = 0.90
_DEFAULT_UMBRAL_WARN = 0.70

_LIBRO_A_CUENTAS_KEY = {
    "20_COMPRAS_GASTOS":   "cuentas_validas_compras",
    "21_VENTAS_INGRESOS":  "cuentas_validas_ventas",
    "22_BIENES_INVERSION": "cuentas_validas_inmovilizado",
}

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
    tokens: int = 0


@dataclass
class ResolucionSemantica:
    valor_final: Optional[str]
    fuente_final: str
    confianza_final: float
    decision: str  # auto, warn, pendiente, block
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


_SYSTEM_CLASIFICADOR_PGC = """\
Eres un clasificador contable experto en el Plan General Contable (PGC) espanol para el sector HORECA.
Tu tarea: dado el texto OCR de una factura y una lista de cuentas disponibles, clasificar la operacion
eligiendo la cuenta y concepto mas adecuados.

REGLAS ABSOLUTAS:
1. Devuelve UNICAMENTE un objeto JSON valido. Sin texto adicional, sin markdown.
2. Solo puedes elegir un concepto y cuenta de la lista de opciones proporcionada.
3. No puedes inventar cuentas ni conceptos que no esten en la lista.
4. Si ninguna opcion encaja con claridad, devuelve concepto: null, cuenta: null.
5. La confianza es tu estimacion interna de 0.0 a 1.0.

DESAMBIGUACION CRITICA HORECA - cuentas 600 / 601 / 602:
- 600 (mercaderias): el bien llega del proveedor y se VENDE o SIRVE SIN NINGUNA TRANSFORMACION.
  Ejemplo: botellas de cerveza, vino embotellado, latas de refresco, agua, tabaco, snacks envasados.
- 601 (materias_primas): el bien se TRANSFORMA EN COCINA antes de venderlo.
  Ejemplo: carne cruda, pescado fresco, verduras, fruta, harina, huevos, aceite de cocinar.
- 602 (aprovisionamientos): el bien SE CONSUME en el proceso pero NO se vende al cliente.
  Ejemplo: envases, bolsas, servilletas, pajitas, vajilla desechable, productos de limpieza.

REGLA DE DESEMPATE 600 vs 601 (OBLIGATORIA):
Si tras analizar la factura NO ESTAS COMPLETAMENTE SEGURO de si el bien se transforma
en cocina (601) o se vende/sirve tal cual (600) -- por ejemplo descripciones genericas
como "ALIMENTACION", "PRODUCTOS VARIOS", "BEBIDAS Y COMESTIBLES", "GENEROS", o cualquier
caso ambiguo donde no se identifique inequivocamente la naturaleza del producto --
DEBES elegir SIEMPRE la cuenta 600 (mercaderias). Solo asigna 601 cuando la factura
identifique INEQUIVOCAMENTE ingredientes crudos para elaboracion (carne fresca, pescado
fresco, verdura cruda, harina, huevos, lacteos crudos, especias a granel). En la duda,
prevalece 600.

FORMATO EXACTO DEL CAMPO "concepto":
- Debe ser EXACTAMENTE la clave entre parentesis mostrada en la lista (p.ej. "mercaderias").
- Solo letras minusculas y guion_bajo. NUNCA incluyas numeros, mayusculas, espacios ni simbolos.
- Correcto:   "mercaderias", "materias_primas", "aprovisionamientos", "suministros"
- Incorrecto: "600", "Mercaderias", "materias primas", "mercaderias_600", "Compras"

Formato de respuesta:
{
  "concepto": "<clave exacta entre parentesis o null>",
  "cuenta_contable": "<codigo de cuenta o null>",
  "justificacion": "<razon concisa en una linea>",
  "confianza": <float 0.0-1.0>
}
""".strip()


def _normalizar_concepto_clave(concepto: Optional[str]) -> Optional[str]:
    """Normalizar la clave de concepto devuelta por el LLM.

    Elimina numeros, simbolos, espacios y capitalización incorrecta.
    Devuelve None si la cadena resultante esta vacia.

    Ejemplos:
        "mercaderias_600" -> "mercaderias"
        "Materias Primas" -> "materias_primas"
        "600"             -> None   (solo numeros, sin letras validas)
        None              -> None
    """
    if not concepto:
        return None
    # Espacios y guiones a guion_bajo
    normalized = concepto.strip().replace(" ", "_").replace("-", "_")
    # Eliminar todo salvo letras y guion_bajo
    normalized = re.sub(r"[^a-zA-Z_]", "", normalized)
    # Minusculas
    normalized = normalized.lower().strip("_")
    # Colapsar guiones_bajos multiples
    normalized = re.sub(r"_+", "_", normalized)
    return normalized if normalized else None


def _cuenta_a_concepto(maestro: dict) -> dict[str, str]:
    """Construir mapa cuenta_code -> concepto_key desde el maestro v3.

    Usado para:
    1. Mostrar claves en el prompt del LLM.
    2. Derivar concepto cuando el LLM devuelve clave invalida pero cuenta correcta.

    Ignora conceptos cuya cuenta sea null (e.g. "no_clasificado").
    """
    conceptos = maestro.get("conceptos", {})
    return {
        v["cuenta"]: key
        for key, v in conceptos.items()
        if isinstance(v, dict) and v.get("cuenta")
    }


def _construir_prompt_clasificacion(
    texto_ocr: str,
    nombre_emisor: str,
    libro: str,
    cuentas_filtradas: list[dict],
    cuenta_a_concepto_map: Optional[dict[str, str]] = None,
) -> str:
    """Construir prompt para clasificacion semantica LLM.

    Muestra la clave de concepto entre parentesis junto a cada cuenta
    para que el LLM devuelva el valor exacto sin inventar nombres.
    """
    cmap = cuenta_a_concepto_map or {}
    lines = [
        f"Libro contable: {libro}",
        f"Emisor: {nombre_emisor}" if nombre_emisor else "",
        "",
        "Texto OCR de la factura (primeros 800 chars):",
        texto_ocr[:800],
        "",
        "Cuentas disponibles — devuelve EXACTAMENTE la clave entre parentesis:",
    ]
    for cuenta in cuentas_filtradas:
        concepto_key = cmap.get(cuenta["code"], "")
        clave_str = f' (concepto: "{concepto_key}")' if concepto_key else ""
        lines.append(
            f"  {cuenta['code']}{clave_str} - {cuenta['label']}: {cuenta['descripcion']}"
        )
    lines.append("")
    lines.append("Cual es el concepto y la cuenta contable de esta factura?")
    return "\n".join(lines)


def _llamar_llm_clasificador(
    texto_ocr: str,
    nombre_emisor: str,
    libro: str,
    cuentas_filtradas: list[dict],
    config: object,
    maestro: Optional[dict] = None,
) -> Optional[CandidatoSemantico]:
    """
    Llamar a Gemini para clasificacion semantica.

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

        cuenta_mapa = _cuenta_a_concepto(maestro) if maestro else {}
        prompt = _construir_prompt_clasificacion(
            texto_ocr, nombre_emisor, libro, cuentas_filtradas, cuenta_mapa
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
            logger.warning("[semantica] LLM devolvio respuesta vacia")
            return None

        data = json.loads(response.text)
        concepto_raw = data.get("concepto")
        cuenta = data.get("cuenta_contable")
        confianza = float(data.get("confianza", 0.0))

        tokens = 0
        if response.usage_metadata:
            tokens = (
                getattr(response.usage_metadata, "prompt_token_count", 0)
                + getattr(response.usage_metadata, "candidates_token_count", 0)
            )

        if concepto_raw is None and cuenta is None:
            logger.info("[semantica] LLM no pudo clasificar (null)")
            return None

        cuentas_validas_set = {c["code"] for c in cuentas_filtradas}
        if cuenta and cuenta not in cuentas_validas_set:
            logger.warning(
                f"[semantica] LLM propuso cuenta '{cuenta}' fuera de opciones del libro, descartando"
            )
            return None

        # Normalizar clave de concepto: quitar numeros, simbolos, mayusculas
        concepto = _normalizar_concepto_clave(concepto_raw)

        # Si el concepto normalizado no es una clave valida del maestro, derivarlo de la cuenta
        conceptos_validos = set(maestro.get("conceptos", {}).keys()) if maestro else set()
        if concepto and conceptos_validos and concepto not in conceptos_validos:
            concepto_derivado = (cuenta_mapa.get(cuenta) if cuenta else None)
            logger.warning(
                f"[semantica] LLM concepto '{concepto_raw}' -> normalizado '{concepto}' "
                f"no es clave valida; derivando de cuenta '{cuenta}' -> '{concepto_derivado}'"
            )
            concepto = concepto_derivado

        # Cap confianza a 0.92: LLM puede AUTO, pero nunca supera al proveedor conocido (0.95)
        confianza = min(confianza, 0.92)

        logger.info(
            f"[semantica] LLM clasifico: concepto={concepto} cuenta={cuenta} "
            f"confianza={confianza:.2f} tokens={tokens}"
        )

        return CandidatoSemantico(
            concepto=concepto,
            cuentacontable=cuenta,
            confianza=confianza,
            fuente="llm_pgc",
            motivo=data.get("justificacion", "Clasificacion LLM PGC"),
            tokens=tokens,
        )

    except Exception as e:
        logger.error(f"[semantica] Error LLM: {e}", exc_info=True)
        return None


def _cargar_conceptos_con_review(maestro_path: str) -> dict[str, bool]:
    """
    Extraer human_review del maestro v3 para cada concepto.

    Lee de maestro['conceptos'][key]['human_review'].

    Returns:
        Dict concepto_key -> human_review (bool)
    """
    maestro = cargar_maestro_contable(maestro_path)
    if not maestro:
        return {}
    conceptos = maestro.get("conceptos", {})
    return {
        key: bool(v.get("human_review", False))
        for key, v in conceptos.items()
        if isinstance(v, dict)
    }


def _cargar_cuentas_validas(maestro_cuentas_path: str) -> dict[str, set[str]]:
    """
    Cargar maestro_cuentas.yaml -> whitelist de cuentas por tipo de libro.

    El archivo usa formato no-estandar con punto y coma como separador.

    Returns:
        Dict tipo -> set de codigos de cuenta validos.
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
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.endswith(":") and not stripped.startswith("-"):
                    current_key = stripped[:-1].strip()
                    result[current_key] = set()
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
    """Validar que la cuenta propuesta esta en la whitelist del libro."""
    if not cuenta:
        return False, "Cuenta contable vacia"

    cuentas_key = _LIBRO_A_CUENTAS_KEY.get(libro)
    if not cuentas_key:
        return True, f"Libro '{libro}' no tiene whitelist definida"

    whitelist = cuentas_validas.get(cuentas_key, set())
    if not whitelist:
        return True, f"Whitelist vacia para {cuentas_key}"

    if cuenta in whitelist:
        return True, f"Cuenta {cuenta} valida para libro {libro}"
    # Sub-cuentas analíticas (p.ej. 705.01) son válidas si su cuenta madre
    # está whitelisted. El catálogo (maestro_contable_fiscal.yaml) define
    # sub-cuentas; la whitelist (maestro_cuentas.yaml) solo lista la madre.
    parent = cuenta.split(".", 1)[0]
    if parent != cuenta and parent in whitelist:
        return True, f"Cuenta {cuenta} válida (cuenta madre {parent} en whitelist de {libro})"
    return False, f"Cuenta {cuenta} no esta en whitelist de {libro}"


def _verificar_coherencia_libro(cuenta: str | None, libro: str) -> tuple[bool, str]:
    """Verificar que el grupo PGC de la cuenta es coherente con el libro."""
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
    return "BIENES_INVERSION" in libro.upper()


_PRIORIDAD = {"block": 3, "warn": 2, "pendiente": 1, "auto": 0}


def resolver_semantica(
    texto_ocr: str,
    nif_emisor: Optional[str],
    nombre_emisor: Optional[str],
    libro: str,
    proveedores_path: str,
    maestro_contable_path: str,
    maestro_cuentas_path: str = "data/maestros/maestro_cuentas.yaml",
    config: Optional[object] = None,
) -> ResultadoSemantico:
    """
    Resolver concepto y cuenta_contable para una factura.

    Flujo:
      1. Proveedor conocido (maestro_proveedores)
      2. LLM con lista limpia de cuentas del libro
      3. Fallback a pendiente si LLM falla o config=None
      4. Validaciones deterministicas

    Args:
        texto_ocr: Texto completo OCR del documento
        nif_emisor: NIF del emisor
        nombre_emisor: Nombre del emisor
        libro: Libro contable destino (e.g. "20_COMPRAS_GASTOS")
        proveedores_path: Ruta a maestro_proveedores.yaml
        maestro_contable_path: Ruta a maestro_contable_fiscal.yaml (v3)
        maestro_cuentas_path: Ruta a maestro_cuentas.yaml
        config: Settings del pipeline (para LLM). None = sin LLM.

    Returns:
        ResultadoSemantico con resolucion de ambos campos
    """
    proveedores = cargar_maestro_proveedores(proveedores_path)
    maestro = cargar_maestro_contable(maestro_contable_path)
    review_flags = _cargar_conceptos_con_review(maestro_contable_path)
    cuentas_validas = _cargar_cuentas_validas(maestro_cuentas_path)

    umbral_auto = getattr(config, "semantica_umbral_confianza_auto", None) or _DEFAULT_UMBRAL_AUTO
    umbral_warn = getattr(config, "semantica_umbral_confianza_warn", None) or _DEFAULT_UMBRAL_WARN

    candidatos: list[CandidatoSemantico] = []
    llm_usado = False
    tokens_llm = 0

    # Fuente 1: Proveedor conocido (maxima prioridad)
    resultado_proveedor = buscar_por_nif(nif_emisor or "", proveedores)
    if resultado_proveedor:
        cand = CandidatoSemantico(
            concepto=resultado_proveedor["concepto"],
            cuentacontable=resultado_proveedor["cuentacontable"],
            confianza=resultado_proveedor["confianza"],
            fuente="maestro_proveedores",
            motivo=f"Proveedor conocido NIF={nif_emisor}",
            tokens=0,
        )
        candidatos.append(cand)
        logger.info(
            f"[semantica] Proveedor conocido: {nif_emisor} -> "
            f"concepto={cand.concepto} cuenta={cand.cuentacontable}"
        )

    # Fuente 2: LLM clasificador (si no hay candidato de alta confianza)
    mejor_confianza = max((c.confianza for c in candidatos), default=0.0)
    if mejor_confianza < umbral_auto and config is not None:
        cuentas_filtradas = cuentas_para_libro(maestro, libro)
        if cuentas_filtradas:
            logger.info(
                f"[semantica] Mejor confianza={mejor_confianza:.2f} < {umbral_auto}, "
                f"invocando LLM con {len(cuentas_filtradas)} cuentas"
            )
            resultado_llm = _llamar_llm_clasificador(
                texto_ocr, nombre_emisor or "", libro, cuentas_filtradas, config,
                maestro=maestro,
            )
            if resultado_llm:
                # Normalizar clave de concepto (proteccion extra ante mocks o LLM ruidoso)
                concepto_limpio = _normalizar_concepto_clave(resultado_llm.concepto)
                conceptos_validos = set(maestro.get("conceptos", {}).keys()) if maestro else set()
                cuenta_mapa_local = _cuenta_a_concepto(maestro) if maestro else {}
                if concepto_limpio and conceptos_validos and concepto_limpio not in conceptos_validos:
                    concepto_derivado = cuenta_mapa_local.get(resultado_llm.cuentacontable)
                    logger.warning(
                        f"[semantica] Candidato LLM concepto '{resultado_llm.concepto}' "
                        f"normalizado '{concepto_limpio}' invalido; "
                        f"derivado de cuenta '{resultado_llm.cuentacontable}' -> '{concepto_derivado}'"
                    )
                    resultado_llm.concepto = concepto_derivado
                elif concepto_limpio != resultado_llm.concepto:
                    resultado_llm.concepto = concepto_limpio

                llm_usado = True
                tokens_llm = resultado_llm.tokens
                candidatos.append(resultado_llm)
        else:
            logger.warning(f"[semantica] No hay cuentas filtradas para libro={libro}")

    # Seleccionar mejor candidato
    concepto_res = _seleccionar_mejor(candidatos, "concepto", review_flags, umbral_auto, umbral_warn)
    cuenta_res = _seleccionar_mejor_cuenta(candidatos, concepto_res.valor_final, umbral_auto, umbral_warn)

    # Validacion: cuenta en whitelist del libro
    cuenta_valida, motivo_cuenta = _validar_cuenta_en_libro(
        cuenta_res.valor_final, libro, cuentas_validas
    )
    if not cuenta_valida and cuenta_res.valor_final is not None:
        logger.warning(f"[semantica] {motivo_cuenta}")
        cuenta_res.decision = "block"
        cuenta_res.motivo = motivo_cuenta

    # Validacion: coherencia grupo PGC con libro
    coherente, motivo_coherencia = _verificar_coherencia_libro(cuenta_res.valor_final, libro)
    if not coherente:
        logger.warning(f"[semantica] {motivo_coherencia}")
        if _PRIORIDAD.get(cuenta_res.decision, 0) < _PRIORIDAD["warn"]:
            cuenta_res.decision = "warn"
            cuenta_res.motivo = motivo_coherencia

    # Bienes de inversion -> WARN minimo (nunca AUTO)
    if _es_bienes_inversion(libro):
        motivo_bi = "Bien de inversion requiere revision humana obligatoria (normativa)"
        if _PRIORIDAD.get(concepto_res.decision, 0) < _PRIORIDAD["warn"]:
            concepto_res.decision = "warn"
            concepto_res.motivo += f" — {motivo_bi}"
        if _PRIORIDAD.get(cuenta_res.decision, 0) < _PRIORIDAD["warn"]:
            cuenta_res.decision = "warn"
            cuenta_res.motivo += f" — {motivo_bi}"
        logger.info("[semantica] Libro bienes inversion: forzando WARN minimo")

    # Decision global
    p_concepto = _PRIORIDAD.get(concepto_res.decision, 0)
    p_cuenta = _PRIORIDAD.get(cuenta_res.decision, 0)
    decision_global = concepto_res.decision if p_concepto >= p_cuenta else cuenta_res.decision

    motivos = []
    if concepto_res.decision != "auto":
        motivos.append(f"concepto: {concepto_res.motivo}")
    if cuenta_res.decision != "auto":
        motivos.append(f"cuenta_contable: {cuenta_res.motivo}")

    return ResultadoSemantico(
        concepto=concepto_res,
        cuenta_contable=cuenta_res,
        decision_global=decision_global,
        requiere_revision_humana=decision_global != "auto",
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

    candidatos_sorted = sorted(candidatos, key=lambda c: c.confianza, reverse=True)
    mejor = candidatos_sorted[0]

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

    force_review = review_flags.get(mejor.concepto, False) if mejor.concepto else False

    if mejor.confianza >= umbral_auto and not force_review:
        decision = "auto"
        motivo = mejor.motivo
    elif mejor.confianza >= umbral_warn or force_review:
        decision = "warn"
        motivo = mejor.motivo
        if force_review:
            motivo += " (human_review requerido)"
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
    """Seleccionar mejor candidato para cuenta_contable."""
    if not candidatos:
        return ResolucionSemantica(
            valor_final=None,
            fuente_final="pendiente",
            confianza_final=0.0,
            decision="pendiente",
            motivo="Sin candidatos para cuenta_contable",
            candidatos=[],
        )

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

    decision = "auto" if mejor.confianza >= umbral_auto else "warn"

    return ResolucionSemantica(
        valor_final=mejor.cuentacontable,
        fuente_final=mejor.fuente,
        confianza_final=mejor.confianza,
        decision=decision,
        motivo=mejor.motivo,
        candidatos=candidatos_serial,
    )
