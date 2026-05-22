"""
Verificador fiscal determinista.

Valida aritméticamente el desglose de IVA extraído por OCR.
Lógica pura sin I/O — todo testeable en memoria.

NO infiere. NO corrige. NO usa LLM. NO llama servicios externos.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

# ── Constantes ───────────────────────────────────────────────────────────────

TOLERANCIA_DEFAULT = Decimal("0.02")

DECISION_PRIORIDAD = {"auto": 0, "warn": 1, "pendiente": 2, "block": 3}


class ClasificacionLinea(str, Enum):
    GRAVADA = "gravada"
    TIPO_CERO = "tipo_cero"
    EXENTA = "exenta"
    INVALIDA = "invalida"


# ── Utilidades ───────────────────────────────────────────────────────────────

# Strings que el OCR devuelve cuando no encuentra un valor numérico real.
# Replica `_PLACEHOLDERS_VACIOS` de phase4_ensamblador.ensamblador para que
# fase 3 fiscal limpie el dato ANTES de validar aritmética y NO escriba el
# símbolo espurio en resultado_fiscal.json (de donde viaja al CSV Intermega).
_PLACEHOLDERS_VACIOS = {"-", "+", "%", "--", "N/A", "n/a", "na", "NA", ""}


def safe_decimal(valor: Any) -> Optional[Decimal]:
    """
    Convierte float/int/str a Decimal NO NEGATIVO. None si no parseable.

    Sanitización aplicada para neutralizar ruido del OCR:
      - String solo con placeholder ("-", "N/A", "%"…)        → None
      - String con signo/símbolo delantero ("+", "-", "%")    → se elimina
      - Espacios en blanco                                    → strip
      - Resultado numérico negativo (cualquier fuente)        → abs()

    El último paso (abs) es defensa contra rutas que ya hayan coercionado
    el string a float/Decimal antes de llegar aquí. Las facturas que
    procesa la gestoría nunca llevan importes negativos en su desglose de
    IVA; las rectificativas se modelan con `tipo_factura`, no con signos.
    """
    if valor is None:
        return None
    if isinstance(valor, str):
        stripped = valor.strip()
        if stripped in _PLACEHOLDERS_VACIOS:
            return None
        if stripped and stripped[0] in ("+", "-", "%"):
            stripped = stripped[1:].strip()
        if not stripped:
            return None
        valor = stripped
    try:
        d = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return abs(d)


def _format_importe(valor: Optional[Decimal]) -> Optional[str]:
    """Formatea un importe a string español: dos decimales, coma como separador."""
    if valor is None:
        return None
    return str(valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)).replace(".", ",")


def peor_decision(*decisiones: str) -> str:
    """Retorna la peor decisión según prioridad: auto < warn < pendiente < block."""
    peor = "auto"
    for d in decisiones:
        if DECISION_PRIORIDAD.get(d, 0) > DECISION_PRIORIDAD.get(peor, 0):
            peor = d
    return peor


def extraer_valor(campo_dict: Any) -> Any:
    """Extrae el valor bruto de un campo con estructura {valor, confianza}."""
    if isinstance(campo_dict, dict):
        return campo_dict.get("valor")
    return None


def _extraer_confianza(campo_dict: Any) -> Optional[Decimal]:
    """Extrae la confianza de un campo con estructura {valor, confianza}."""
    if isinstance(campo_dict, dict):
        return safe_decimal(campo_dict.get("confianza"))
    return None


# ── Clasificación de líneas ──────────────────────────────────────────────────

def _clasificar_linea(tipo_raw: Any) -> tuple[ClasificacionLinea, Optional[Decimal]]:
    """
    Clasifica una línea fiscal por su tipo_porcentaje.

    Returns:
        (clasificación, tipo_decimal_o_None)
    """
    if tipo_raw is None:
        return ClasificacionLinea.EXENTA, None

    tipo_dec = safe_decimal(tipo_raw)
    if tipo_dec is None:
        return ClasificacionLinea.INVALIDA, None

    if tipo_dec > 0:
        return ClasificacionLinea.GRAVADA, tipo_dec
    if tipo_dec == 0:
        return ClasificacionLinea.TIPO_CERO, tipo_dec

    # Tipo negativo — inválido
    return ClasificacionLinea.INVALIDA, None


# ── Verificación por línea ───────────────────────────────────────────────────

def _verificar_linea(
    linea: dict,
    indice: int,
    tolerancia: Decimal = TOLERANCIA_DEFAULT,
) -> dict:
    """
    Aplica reglas FISCAL_001..005 a una línea fiscal.

    Recibe un elemento de fiscal.lineas_fiscales (estructura OCR).
    Retorna dict con resultados de cada regla y metadatos de línea.
    """
    reglas = []
    errores = []
    warnings = []

    # Extraer valores brutos
    base_raw = extraer_valor(linea.get("base_euros"))
    tipo_raw = extraer_valor(linea.get("tipo_porcentaje"))
    cuota_raw = extraer_valor(linea.get("cuota"))
    total_linea_raw = extraer_valor(linea.get("total_linea"))

    # Convertir a Decimal
    base = safe_decimal(base_raw)
    cuota = safe_decimal(cuota_raw)
    total_linea = safe_decimal(total_linea_raw)

    # Confianzas
    confianzas = []
    for campo_key in ("base_euros", "tipo_porcentaje", "cuota", "total_linea"):
        c = _extraer_confianza(linea.get(campo_key))
        if c is not None:
            confianzas.append(c)

    # Clasificar línea
    clasificacion, tipo_dec = _clasificar_linea(tipo_raw)

    # ── FISCAL_001: BASE_PRESENTE ────────────────────────────────────────
    if base is not None:
        reglas.append({"codigo": "FISCAL_001", "estado": "ok", "diferencia": None})
    elif base_raw is not None:
        # Presente pero no parseable
        reglas.append({"codigo": "FISCAL_001", "estado": "error",
                       "diferencia": None})
        errores.append(f"Línea {indice}: base_euros no parseable: {base_raw!r}")
    else:
        reglas.append({"codigo": "FISCAL_001", "estado": "no_ejecutada",
                       "diferencia": None})

    # ── FISCAL_002: TIPO_PRESENTE ────────────────────────────────────────
    if clasificacion == ClasificacionLinea.EXENTA:
        reglas.append({"codigo": "FISCAL_002", "estado": "ok",
                       "diferencia": None})
    elif clasificacion == ClasificacionLinea.INVALIDA:
        reglas.append({"codigo": "FISCAL_002", "estado": "error",
                       "diferencia": None})
        errores.append(f"Línea {indice}: tipo_porcentaje no parseable: {tipo_raw!r}")
    else:
        reglas.append({"codigo": "FISCAL_002", "estado": "ok",
                       "diferencia": None})

    # ── FISCAL_003: CUOTA_PRESENTE ───────────────────────────────────────
    if cuota is not None:
        reglas.append({"codigo": "FISCAL_003", "estado": "ok", "diferencia": None})
    elif cuota_raw is not None:
        reglas.append({"codigo": "FISCAL_003", "estado": "error",
                       "diferencia": None})
        errores.append(f"Línea {indice}: cuota no parseable: {cuota_raw!r}")
    else:
        reglas.append({"codigo": "FISCAL_003", "estado": "no_ejecutada",
                       "diferencia": None})

    # ── FISCAL_004: CUOTA_EQ_BASE_TIPO ───────────────────────────────────
    cuota_recalculada = None
    if clasificacion == ClasificacionLinea.EXENTA:
        reglas.append({"codigo": "FISCAL_004", "estado": "no_ejecutada",
                       "diferencia": None})
    elif clasificacion == ClasificacionLinea.INVALIDA:
        reglas.append({"codigo": "FISCAL_004", "estado": "error",
                       "diferencia": None})
    elif base is not None and tipo_dec is not None and cuota is not None:
        cuota_recalculada = (base * tipo_dec / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        diff = abs(cuota - cuota_recalculada)
        if diff <= tolerancia:
            reglas.append({"codigo": "FISCAL_004", "estado": "ok",
                           "diferencia": _format_importe(diff)})
        else:
            reglas.append({"codigo": "FISCAL_004", "estado": "fail",
                           "diferencia": _format_importe(diff)})
            errores.append(
                f"Línea {indice}: cuota={cuota} vs recalculada={cuota_recalculada} "
                f"(diff={diff}, tolerancia={tolerancia})"
            )
    else:
        reglas.append({"codigo": "FISCAL_004", "estado": "no_ejecutada",
                       "diferencia": None})

    # Para TIPO_CERO: cuota_recalculada = 0.00
    if clasificacion == ClasificacionLinea.TIPO_CERO and cuota_recalculada is None:
        cuota_recalculada = Decimal("0.00")

    # ── FISCAL_005: TOTAL_LINEA_OK ───────────────────────────────────────
    total_linea_recalculado = None
    if total_linea is None:
        reglas.append({"codigo": "FISCAL_005", "estado": "no_ejecutada",
                       "diferencia": None})
    elif clasificacion == ClasificacionLinea.INVALIDA:
        reglas.append({"codigo": "FISCAL_005", "estado": "error",
                       "diferencia": None})
    elif clasificacion == ClasificacionLinea.EXENTA:
        # Exenta: total_linea ≈ base (sin cuota)
        if base is not None:
            total_linea_recalculado = base
            diff = abs(total_linea - base)
            if diff <= tolerancia:
                reglas.append({"codigo": "FISCAL_005", "estado": "ok",
                               "diferencia": _format_importe(diff)})
            else:
                reglas.append({"codigo": "FISCAL_005", "estado": "fail",
                               "diferencia": _format_importe(diff)})
                errores.append(
                    f"Línea {indice} exenta: total_linea={total_linea} vs base={base} "
                    f"(diff={diff})"
                )
        else:
            reglas.append({"codigo": "FISCAL_005", "estado": "no_ejecutada",
                           "diferencia": None})
    else:
        # Gravada o Tipo cero
        cuota_para_total = cuota_recalculada if cuota_recalculada is not None else cuota
        if base is not None and cuota_para_total is not None:
            total_linea_recalculado = base + cuota_para_total
            diff = abs(total_linea - total_linea_recalculado)
            if diff <= tolerancia:
                reglas.append({"codigo": "FISCAL_005", "estado": "ok",
                               "diferencia": _format_importe(diff)})
            else:
                reglas.append({"codigo": "FISCAL_005", "estado": "fail",
                               "diferencia": _format_importe(diff)})
                errores.append(
                    f"Línea {indice}: total_linea={total_linea} vs "
                    f"recalculado={total_linea_recalculado} (diff={diff})"
                )
        else:
            reglas.append({"codigo": "FISCAL_005", "estado": "no_ejecutada",
                           "diferencia": None})

    # ── Decisión de línea ────────────────────────────────────────────────
    if clasificacion == ClasificacionLinea.INVALIDA:
        decision_linea = "block"
    elif clasificacion == ClasificacionLinea.EXENTA:
        # Exenta siempre es warn como mínimo
        tiene_fail = any(r["estado"] == "fail" for r in reglas)
        tiene_error = any(r["estado"] == "error" for r in reglas)
        if tiene_error or tiene_fail:
            decision_linea = "block"
        else:
            decision_linea = "warn"
            warnings.append(f"Línea {indice}: línea exenta detectada — requiere revisión humana")
    else:
        # Gravada o Tipo cero
        tiene_fail = any(r["estado"] == "fail" for r in reglas)
        tiene_error = any(r["estado"] == "error" for r in reglas)
        if tiene_error or tiene_fail:
            decision_linea = "block"
        else:
            decision_linea = "auto"

    confianza_min = min(confianzas) if confianzas else Decimal("0")

    return {
        "indice": indice,
        "clasificacion": clasificacion.value,
        "base_observada": _format_importe(base),
        "tipo_observado": _format_importe(tipo_dec),
        "cuota_observada": _format_importe(cuota),
        "total_linea_observado": _format_importe(total_linea),
        "cuota_recalculada": _format_importe(cuota_recalculada),
        "total_linea_recalculado": _format_importe(total_linea_recalculado),
        "reglas": reglas,
        "decision_linea": decision_linea,
        "confianza_minima": confianza_min,
        "errores": errores,
        "warnings": warnings,
        # Para cálculos de factura
        "_base_decimal": base,
        "_cuota_recalculada_decimal": cuota_recalculada if cuota_recalculada is not None else (
            cuota if cuota is not None else Decimal("0")
        ),
        "_clasificacion_enum": clasificacion,
    }


# ── Verificación por factura ─────────────────────────────────────────────────

def verificar_fiscal(
    fiscal_data: dict,
    tolerancia: Decimal = TOLERANCIA_DEFAULT,
) -> dict:
    """
    Entry point principal. Función pura, sin I/O.

    Args:
        fiscal_data: sección "fiscal" de documento_extraido.json
        tolerancia: tolerancia aritmética en euros (default 0.02)

    Returns:
        dict con estructura completa de resultado_fiscal.json
    """
    errores_factura: list[str] = []
    warnings_factura: list[str] = []
    reglas_factura: list[dict] = []

    # ── Extraer total_euros ──────────────────────────────────────────────
    total_euros_campo = fiscal_data.get("total_euros", {})
    total_euros_raw = extraer_valor(total_euros_campo)
    total_euros = safe_decimal(total_euros_raw)
    total_euros_confianza = _extraer_confianza(total_euros_campo)

    # ── Propagar requiere_revision del OCR ───────────────────────────────
    requiere_revision_ocr = bool(fiscal_data.get("requiere_revision", False))
    if requiere_revision_ocr:
        warnings_factura.append("OCR marcó requiere_revision=True en sección fiscal")

    # ── Extraer y verificar líneas ───────────────────────────────────────
    lineas_raw = fiscal_data.get("lineas_fiscales", [])

    # FISCAL_006: LINEAS_PRESENTES
    if not lineas_raw:
        reglas_factura.append({
            "codigo": "FISCAL_006", "estado": "fail",
            "observado": "0", "recalculado": None, "diferencia": None,
        })
    else:
        reglas_factura.append({
            "codigo": "FISCAL_006", "estado": "ok",
            "observado": str(len(lineas_raw)), "recalculado": None,
            "diferencia": None,
        })

    # Verificar cada línea
    lineas_verificadas = []
    for i, linea in enumerate(lineas_raw):
        resultado_linea = _verificar_linea(linea, i, tolerancia)
        lineas_verificadas.append(resultado_linea)
        errores_factura.extend(resultado_linea["errores"])
        warnings_factura.extend(resultado_linea["warnings"])

    # ── FISCAL_007: TOTAL_FACTURA_OK ─────────────────────────────────────
    if total_euros is not None and lineas_verificadas:
        suma_recalculada = Decimal("0")
        for lv in lineas_verificadas:
            base_lv = lv["_base_decimal"]
            cuota_lv = lv["_cuota_recalculada_decimal"]
            if base_lv is not None:
                suma_recalculada += base_lv + (cuota_lv if cuota_lv is not None else Decimal("0"))

        diff_total = abs(total_euros - suma_recalculada)
        if diff_total <= tolerancia:
            reglas_factura.append({
                "codigo": "FISCAL_007", "estado": "ok",
                "observado": _format_importe(total_euros),
                "recalculado": _format_importe(suma_recalculada),
                "diferencia": _format_importe(diff_total),
            })
        else:
            reglas_factura.append({
                "codigo": "FISCAL_007", "estado": "fail",
                "observado": _format_importe(total_euros),
                "recalculado": _format_importe(suma_recalculada),
                "diferencia": _format_importe(diff_total),
            })
            errores_factura.append(
                f"Total factura: observado={total_euros} vs recalculado={suma_recalculada} "
                f"(diff={diff_total}, tolerancia={tolerancia})"
            )
    elif total_euros is None:
        reglas_factura.append({
            "codigo": "FISCAL_007", "estado": "no_ejecutada",
            "observado": None, "recalculado": None, "diferencia": None,
        })
    else:
        # total_euros presente pero sin líneas
        reglas_factura.append({
            "codigo": "FISCAL_007", "estado": "no_ejecutada",
            "observado": str(total_euros), "recalculado": None,
            "diferencia": None,
        })

    # ── FISCAL_008: MULTITRAMO_OK ────────────────────────────────────────
    tipos_gravados = set()
    for lv in lineas_verificadas:
        cls = lv["_clasificacion_enum"]
        if cls in (ClasificacionLinea.GRAVADA, ClasificacionLinea.TIPO_CERO):
            tipo_obs = lv["tipo_observado"]
            if tipo_obs is not None:
                tipos_gravados.add(tipo_obs)

    if len(tipos_gravados) > 1:
        reglas_factura.append({
            "codigo": "FISCAL_008", "estado": "ok",
            "observado": str(len(tipos_gravados)),
            "recalculado": None,
            "diferencia": None,
        })
    elif len(tipos_gravados) <= 1:
        reglas_factura.append({
            "codigo": "FISCAL_008", "estado": "ok",
            "observado": str(len(tipos_gravados)),
            "recalculado": None,
            "diferencia": None,
        })

    # ── Clasificación resumen ────────────────────────────────────────────
    clasificacion_resumen = {
        "gravadas": 0, "tipo_cero": 0, "exentas": 0, "invalidas": 0,
    }
    for lv in lineas_verificadas:
        cls = lv["_clasificacion_enum"]
        if cls == ClasificacionLinea.GRAVADA:
            clasificacion_resumen["gravadas"] += 1
        elif cls == ClasificacionLinea.TIPO_CERO:
            clasificacion_resumen["tipo_cero"] += 1
        elif cls == ClasificacionLinea.EXENTA:
            clasificacion_resumen["exentas"] += 1
        elif cls == ClasificacionLinea.INVALIDA:
            clasificacion_resumen["invalidas"] += 1

    # ── Decisiones de campos ─────────────────────────────────────────────

    # decision de lineas_fiscales
    if not lineas_raw:
        decision_lineas = "pendiente"
        motivo_lineas = "Sin líneas fiscales — datos insuficientes"
        confianza_lineas = None
    else:
        decisiones_lineas = [lv["decision_linea"] for lv in lineas_verificadas]
        decision_lineas = peor_decision(*decisiones_lineas)
        # FISCAL_008 no cambia decisión (informativo)
        motivo_partes = []
        for lv in lineas_verificadas:
            motivo_partes.append(
                f"línea {lv['indice']}: {lv['clasificacion']} → {lv['decision_linea']}"
            )
        motivo_lineas = "; ".join(motivo_partes)
        confianzas_lineas = [lv["confianza_minima"] for lv in lineas_verificadas]
        confianza_lineas = min(confianzas_lineas) if confianzas_lineas else Decimal("0")

    # decision de total_euros
    if total_euros is None:
        decision_total = "pendiente"
        motivo_total = "total_euros ausente — datos insuficientes"
        confianza_total = None
    else:
        regla_007 = next((r for r in reglas_factura if r["codigo"] == "FISCAL_007"), None)
        if regla_007 and regla_007["estado"] == "fail":
            decision_total = "block"
            motivo_total = (
                f"FISCAL_007 FAIL: total={regla_007['observado']} vs "
                f"recalculado={regla_007['recalculado']} (diff={regla_007['diferencia']})"
            )
        elif regla_007 and regla_007["estado"] == "ok":
            decision_total = "auto"
            motivo_total = f"FISCAL_007 OK (diff={regla_007['diferencia']})"
        else:
            # no_ejecutada (sin líneas)
            decision_total = "pendiente"
            motivo_total = "FISCAL_007 no ejecutada — sin líneas para comparar"

        confianza_total = total_euros_confianza

    # decision_global
    decision_global = peor_decision(decision_total, decision_lineas)

    requiere_revision_humana = decision_global != "auto"
    motivos_revision = []
    if decision_global != "auto":
        if decision_total != "auto":
            motivos_revision.append(f"total_euros: {motivo_total}")
        if decision_lineas != "auto":
            motivos_revision.append(f"lineas_fiscales: {motivo_lineas}")

    # ── Construir bloques_fiscales (sin claves internas) ─────────────────
    bloques_fiscales = []
    for lv in lineas_verificadas:
        bloque = {
            "indice": lv["indice"],
            "clasificacion": lv["clasificacion"],
            "base_observada": lv["base_observada"],
            "tipo_observado": lv["tipo_observado"],
            "cuota_observada": lv["cuota_observada"],
            "total_linea_observado": lv["total_linea_observado"],
            "cuota_recalculada": lv["cuota_recalculada"],
            "total_linea_recalculado": lv["total_linea_recalculado"],
            "reglas": lv["reglas"],
        }
        bloques_fiscales.append(bloque)

    # ── Valor final de lineas_fiscales (lista de dicts enriquecidos) ─────
    lineas_valor_final = []
    for lv in lineas_verificadas:
        lineas_valor_final.append({
            "base_euros": lv["base_observada"],
            "tipo_porcentaje": lv["tipo_observado"],
            "cuota": lv["cuota_observada"],
            "cuota_recalculada": lv["cuota_recalculada"],
            "total_linea": lv["total_linea_observado"],
            "clasificacion": lv["clasificacion"],
            "decision_linea": lv["decision_linea"],
        })

    # ── Serializar confianzas ────────────────────────────────────────────
    confianza_total_out = (
        round(Decimal(str(confianza_total)), 4) if confianza_total is not None else None
    )
    confianza_lineas_out = (
        round(Decimal(str(confianza_lineas)), 4) if confianza_lineas is not None else None
    )

    # ── Resultado final ──────────────────────────────────────────────────
    return {
        "campos": {
            "total_euros": {
                "valor_final": _format_importe(total_euros),
                "fuente_final": "fiscal_verificado",
                "confianza_final": confianza_total_out,
                "decision": decision_total,
                "motivo": motivo_total,
                "llm_usado": False,
                "candidatos": [],
            },
            "lineas_fiscales": {
                "valor_final": lineas_valor_final if lineas_valor_final else None,
                "fuente_final": "fiscal_verificado",
                "confianza_final": confianza_lineas_out,
                "decision": decision_lineas,
                "motivo": motivo_lineas,
                "llm_usado": False,
                "candidatos": [],
            },
        },
        "decision_global": decision_global,
        "requiere_revision_humana": requiere_revision_humana,
        "motivos_revision": motivos_revision,
        "tolerancia": _format_importe(tolerancia),
        "requiere_revision_ocr": requiere_revision_ocr,
        "clasificacion_lineas": clasificacion_resumen,
        "bloques_fiscales": bloques_fiscales,
        "reglas_factura": reglas_factura,
        "errores": errores_factura,
        "warnings": warnings_factura,
    }
