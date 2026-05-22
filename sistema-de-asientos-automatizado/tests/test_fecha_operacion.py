"""
Tests para resolver_fecha_operacion (hallazgo F3).

La factura no trae fecha de operación; el sistema la fabricaba con la fecha
de proceso (hoy) y confianza 1.0 (AUTO), violando "no inventar datos". La fecha
de operación debe derivar de fecha_expedicion (mismo valor/confianza/decisión)
o quedar pendiente si no hay expedición — nunca la fecha del sistema con AUTO 1.0.
"""

from datetime import datetime, timezone

from src.phase3_identidad_cabecera.field_resolvers import resolver_fecha_operacion
from src.phase3_identidad_cabecera.field_candidate import (
    FieldResolution, FuenteCandidato, DecisionCampo,
)


def _exp(valor, conf=0.93, decision=DecisionCampo.AUTO) -> FieldResolution:
    return FieldResolution(
        campo="fecha_expedicion",
        valor_final=valor,
        fuente_final=FuenteCandidato.NORMALIZED_VALUE if valor else None,
        confianza_final=conf if valor else 0.0,
        decision=decision,
        motivo="test",
    )


class TestFechaOperacion:

    def test_deriva_de_fecha_expedicion(self):
        res = resolver_fecha_operacion(_exp("2024-01-31", conf=0.93))
        assert res.valor_final == "2024-01-31"
        assert res.confianza_final == 0.93
        assert res.confianza_final <= 0.95
        # Hereda la decisión de la expedición resuelta.
        assert res.decision == DecisionCampo.AUTO

    def test_no_fabrica_fecha_del_sistema_con_auto_1(self):
        res = resolver_fecha_operacion(_exp("2024-01-31", conf=0.93))
        hoy = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        assert res.confianza_final != 1.0
        assert res.valor_final != hoy

    def test_sin_expedicion_queda_pendiente(self):
        res = resolver_fecha_operacion(_exp(None))
        assert res.valor_final is None
        assert res.decision == DecisionCampo.PENDIENTE

    def test_sin_argumento_no_inventa(self):
        # Backward-compat: llamada sin argumento no debe fabricar la fecha de hoy.
        res = resolver_fecha_operacion()
        assert res.valor_final is None
        assert res.decision == DecisionCampo.PENDIENTE
