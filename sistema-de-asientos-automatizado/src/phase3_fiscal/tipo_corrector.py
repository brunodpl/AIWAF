"""
Corrector determinista de tipo de IVA mal extraído (hallazgo F2).

Cuando la columna "Tipo(%)" del documento no es numérica (p.ej. una letra B/BH),
Gemini a veces toma el IMPORTE del IVA como si fuera el PORCENTAJE. Si el tipo
extraído no es un tipo legal pero la aritmética cuota/base reconcilia con un tipo
legal dentro de tolerancia, este módulo sustituye el tipo y baja la confianza.

Lógica pura y determinista (Decimal, ROUND_HALF_UP). NO usa LLM, NO hace I/O.
Vive fuera del verificador a propósito: el verificador NO corrige, solo valida.
La corrección se aplica en el orquestador de fase 3 fiscal ANTES de validar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Sequence

from .verificador import _extraer_valor, _safe_decimal

# Confianza asignada al tipo tras una corrección heurística: por debajo de
# cualquier umbral de autocarga para que el dato llegue a revisión humana.
_CONFIANZA_CORREGIDA = 0.5

# Tolerancia (en puntos porcentuales) al comparar el tipo derivado de cuota/base
# con un tipo legal. Los tipos legales (4/10/21) distan >6 pp entre sí, así que
# 1.0 absorbe ruido de redondeo sin permitir confusiones entre tipos.
_TOLERANCIA_DEFAULT = Decimal("1.0")


@dataclass
class ResultadoCorreccion:
    """Resultado de intentar corregir el tipo de una línea fiscal."""
    corregido: bool
    tipo_original: Optional[Decimal] = None
    tipo_corregido: Optional[Decimal] = None
    motivo: str = ""


def corregir_tipo_iva_invalido(
    linea: dict,
    tipos_legales: Sequence,
    tolerancia: Decimal = _TOLERANCIA_DEFAULT,
) -> ResultadoCorreccion:
    """
    Corrige in-place el ``tipo_porcentaje`` de una línea fiscal si el tipo
    extraído no es legal pero ``cuota/base`` reconcilia con un tipo legal.

    Args:
        linea: elemento de ``fiscal.lineas_fiscales`` ({campo: {valor, confianza}}).
        tipos_legales: tipos de IVA legales (p.ej. [0, 4, 10, 21]).
        tolerancia: máxima diferencia (pp) entre el tipo derivado y un tipo legal.

    Returns:
        ResultadoCorreccion. ``corregido=True`` solo si se sustituyó el tipo.
        No se toca la línea si es exenta, si el tipo ya es legal, o si cuota/base
        no reconcilia con ningún tipo legal (en cuyo caso FISCAL_007 la bloqueará).
    """
    tipo_campo = linea.get("tipo_porcentaje")
    if not isinstance(tipo_campo, dict):
        return ResultadoCorreccion(False)

    tipo_raw = _extraer_valor(tipo_campo)
    if tipo_raw is None:
        return ResultadoCorreccion(False)  # exenta

    tipo = _safe_decimal(tipo_raw)
    if tipo is None or tipo == 0:
        return ResultadoCorreccion(False)  # no parseable o tipo cero → no tocar

    legales = [_safe_decimal(t) for t in tipos_legales]
    legales = [t for t in legales if t is not None]
    if tipo in legales:
        return ResultadoCorreccion(False)  # ya es un tipo legal

    base = _safe_decimal(_extraer_valor(linea.get("base_euros")))
    cuota = _safe_decimal(_extraer_valor(linea.get("cuota")))
    if base is None or base == 0 or cuota is None:
        return ResultadoCorreccion(False)  # sin datos para derivar

    derivado = (cuota / base * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    candidatos = sorted(
        (t for t in legales if t > 0),
        key=lambda t: abs(derivado - t),
    )
    if not candidatos or abs(derivado - candidatos[0]) > tolerancia:
        return ResultadoCorreccion(False)  # no reconcilia → que FISCAL_007 bloquee

    tipo_legal = candidatos[0]
    tipo_campo["valor"] = float(tipo_legal)
    tipo_campo["confianza"] = _CONFIANZA_CORREGIDA

    return ResultadoCorreccion(
        corregido=True,
        tipo_original=tipo,
        tipo_corregido=tipo_legal,
        motivo=(
            f"Tipo IVA {tipo} no es legal; derivado de cuota/base "
            f"({cuota}/{base}≈{derivado}%) → {tipo_legal}% — verificar."
        ),
    )
