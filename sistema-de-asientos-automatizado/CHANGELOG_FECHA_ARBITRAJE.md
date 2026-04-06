# Changelog: Arbitraje de Fechas con Múltiples Entity Types

## Fecha: 2026-03-27

## Problema Resuelto

El resolvedor de `fecha_expedicion` solo buscaba el entity type `invoice_date` de Document AI.
Si la fecha aparecía en el documento como `delivery_date`, `receive_date` u otros tipos,
el sistema la ignoraba completamente, resultando en 0 candidatos y decisión BLOCK.

**Ejemplo Doc 19 GALDIS**: La fecha aparecía como `delivery_date` pero el sistema
solo buscaba `invoice_date`, causando un BLOCK incorrecto.

## Solución Implementada

### 1. Modificación de `_resolver_fecha_generico()` en `field_resolvers.py`

**Cambio de signatura**:
```python
# Antes:
def _resolver_fecha_generico(entity_type: str, ...)

# Después:
def _resolver_fecha_generico(entity_types: list[str], ...)
```

**Comportamiento nuevo**:
- Busca en **todos** los entity types proporcionados (invoice_date, delivery_date, receive_date, due_date)
- Recolecta cada fecha encontrada como un candidato separado con motivo específico
- Elimina duplicados (mismo valor normalizado)
- Preserva la trazabilidad de qué entity type generó cada candidato

### 2. Actualización de `resolver_fecha_expedicion()`

Ahora recolecta candidatos de 4 tipos de fecha:
```python
return _resolver_fecha_generico(
    ["invoice_date", "delivery_date", "receive_date", "due_date"],
    "fecha_expedicion",
    extractor, _CONTEXTO_FECHA_EXP,
    umbral_auto, umbral_warn,
)
```

### 3. Flujo de Arbitraje LLM

Cuando hay **múltiples fechas distintas**:
1. Cada fecha se convierte en un candidato con su confianza y motivo
2. Si hay conflicto (múltiples valores viables), el sistema marca `necesita_llm=True`
3. El `LLMDisambiguator` recibe **todos los candidatos** y elige el correcto basándose en:
   - Tipo de entity (invoice_date tiene prioridad semántica)
   - Confianza de Document AI
   - Contexto del documento
4. El LLM devuelve el **índice** del candidato elegido (no inventa fechas)

## Tests Implementados

Se creó `tests/test_field_resolvers_fecha.py` con 5 tests unitarios:

1. **test_resolver_fecha_expedicion_multiples_tipos**: Verifica que `delivery_date` se recolecta cuando no hay `invoice_date`
2. **test_resolver_fecha_expedicion_sin_duplicados**: Verifica que no se crean candidatos duplicados con el mismo valor
3. **test_resolver_fecha_expedicion_multiples_fechas_distintas**: Verifica que se recolectan 3 fechas diferentes como candidatos separados
4. **test_resolver_fecha_expedicion_solo_invoice_date**: Backward compatibility - comportamiento normal con solo invoice_date
5. **test_resolver_fecha_expedicion_sin_ninguna_fecha**: Verifica BLOCK cuando no hay fechas

**Resultado**: ✅ Todos los tests pasan

## Compatibilidad

- ✅ Tests existentes en `tests/test_identidad_main.py` pasan sin cambios
- ✅ La interfaz pública de `resolver_fecha_expedicion()` no cambió
- ✅ El uso en `cabecera_resolver.py` no requiere modificaciones
- ✅ Backward compatible: documentos con solo `invoice_date` funcionan igual que antes

## Beneficios

1. **Robustez**: El sistema ya no falla cuando la fecha está en un entity type alternativo
2. **Trazabilidad**: Cada candidato conserva el tipo de entidad del que proviene
3. **Arbitraje inteligente**: El LLM puede elegir la fecha correcta cuando hay múltiples opciones
4. **Sin invención de datos**: El LLM solo elige entre candidatos existentes, nunca genera fechas

## Ejemplo de Salida JSON

```json
{
  "campo": "fecha_expedicion",
  "valor_final": "2026-03-15",
  "fuente_final": "llm_arbitro",
  "confianza_final": 0.88,
  "decision": "auto",
  "candidatos": [
    {
      "valor_normalizado": "2026-03-10",
      "fuente": "document_ai_nativo",
      "confianza": 0.85,
      "motivo": "invoice_date Document AI, normalized_value (conf: 0.85)",
      "validacion_ok": true
    },
    {
      "valor_normalizado": "2026-03-15",
      "fuente": "document_ai_nativo",
      "confianza": 0.90,
      "motivo": "delivery_date Document AI, normalized_value (conf: 0.90)",
      "validacion_ok": true
    },
    {
      "valor_normalizado": "2026-03-20",
      "fuente": "document_ai_nativo",
      "confianza": 0.88,
      "motivo": "due_date Document AI, normalized_value (conf: 0.88)",
      "validacion_ok": true
    }
  ],
  "llm_arbitro_usado": true,
  "justificacion_llm": "delivery_date es la fecha correcta de expedición según contexto del documento"
}
```

## Archivos Modificados

- `src/phase3_identidad_cabecera/field_resolvers.py`: Lógica de recolección de múltiples fechas
- `tests/test_field_resolvers_fecha.py`: Nueva suite de tests (creado)

## Archivos Sin Cambios (compatibles)

- `src/phase3_identidad_cabecera/cabecera_resolver.py`: Usa la misma interfaz
- `src/phase3_identidad_cabecera/llm_disambiguator.py`: Recibe candidatos como siempre
- `src/phase3_identidad_cabecera/field_candidate.py`: Contratos de datos sin cambios
