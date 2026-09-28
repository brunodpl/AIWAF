"""
Regresión: CabeceraResult._decision_global debe contemplar PENDIENTE.

Greptile (PR #5) detectó que `_decision_global` solo miraba BLOCK y WARN. Si un
campo OBLIGATORIO devolviera `pendiente` (prioridad entre block y warn según
DECISION_PRIORIDAD), el documento se autocargaba (`auto`) en lugar de escalar a
revisión humana — exactamente lo que las reglas de validación prohíben ("dato ausente no se
fabrica → revisión_humana"). Hoy solo `fecha_operacion` (campo NO obligatorio)
usa PENDIENTE, pero el guard cierra la trampa para cualquier resolver futuro.
"""

from src.phase3_identidad_cabecera.field_candidate import CabeceraResult, DecisionCampo


def _campos(**overrides):
    """Seis campos obligatorios en 'auto' salvo los que se sobreescriban."""
    base = {
        "nif_entidad": "auto",
        "nombre_entidad": "auto",
        "numero_factura": "auto",
        "fecha_expedicion": "auto",
        "nif_receptor": "auto",
        "nombre_receptor": "auto",
    }
    base.update(overrides)
    return {k: {"decision": v} for k, v in base.items()}


def _finalizar(**overrides) -> CabeceraResult:
    return CabeceraResult(documento_id="t", **_campos(**overrides)).finalizar()


def test_obligatorio_pendiente_escala_a_revision():
    r = _finalizar(fecha_expedicion="pendiente")
    assert r.decision_global == DecisionCampo.PENDIENTE.value
    assert r.requiere_revision_humana is True


def test_block_gana_sobre_pendiente():
    r = _finalizar(nif_entidad="block", fecha_expedicion="pendiente")
    assert r.decision_global == DecisionCampo.BLOCK.value


def test_pendiente_gana_sobre_warn():
    r = _finalizar(nombre_entidad="warn", fecha_expedicion="pendiente")
    assert r.decision_global == DecisionCampo.PENDIENTE.value


def test_todo_auto_sigue_auto():
    r = _finalizar()
    assert r.decision_global == DecisionCampo.AUTO.value
    assert r.requiere_revision_humana is False
