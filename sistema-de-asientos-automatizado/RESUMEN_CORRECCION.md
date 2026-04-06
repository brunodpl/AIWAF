# Resumen de Correcciones Implementadas

**Fecha**: 2026-03-25
**Fase**: 3 - Identidad y Cabecera
**Objetivo**: Eliminar `fecha_vencimiento` y añadir fuente `SISTEMA` para `fecha_operacion`

---

## Cambios Implementados

### 1. **field_candidate.py**

#### Añadido `FuenteCandidato.SISTEMA` al enum (línea 29)
```python
class FuenteCandidato(str, Enum):
    DOCUMENT_AI_NATIVO    = "document_ai_nativo"
    NORMALIZED_VALUE      = "normalized_value"
    REGEX_CONTEXTUAL      = "regex_contextual"
    BBOX_PROXIMIDAD       = "bbox_proximidad"
    LLM_ARBITRO           = "llm_arbitro"
    SISTEMA               = "sistema"          # ← NUEVO
    FALLBACK              = "fallback"
```

#### Eliminado atributo `fecha_vencimiento` del dataclass (línea ~141)
```python
# ANTES:
fecha_vencimiento: Optional[dict] = None  # puede ser null

# DESPUÉS:
# (línea eliminada)
```

---

### 2. **field_resolvers.py**

#### Eliminada función `resolver_fecha_vencimiento()` completa (líneas 596-612)
```python
# ANTES:
def resolver_fecha_vencimiento(
    extractor: DocumentAIEntityExtractor,
    umbral_auto: float = _UMBRAL_AUTO,
    umbral_warn: float = _UMBRAL_WARN,
) -> Optional[FieldResolution]:
    # ... implementación ...

# DESPUÉS:
# (función completamente eliminada)
```

#### Cambiada fuente a `SISTEMA` en `resolver_fecha_operacion()` (líneas 618, 625, 633)
```python
# ANTES:
fuente=FuenteCandidato.DOCUMENT_AI_NATIVO

# DESPUÉS:
fuente=FuenteCandidato.SISTEMA
```

#### Eliminada constante `_CONTEXTO_FECHA_VTO` (líneas 109-113)
```python
# ANTES:
_CONTEXTO_FECHA_VTO = re.compile(...)

# DESPUÉS:
# (constante eliminada)
```

#### Añadido `SISTEMA` al diccionario `_prioridad` (línea 224)
```python
_prioridad = {
    FuenteCandidato.DOCUMENT_AI_NATIVO: 0,
    FuenteCandidato.NORMALIZED_VALUE: 1,
    FuenteCandidato.SISTEMA: 0,  # ← NUEVO (misma prioridad que DOCUMENT_AI_NATIVO)
    FuenteCandidato.REGEX_CONTEXTUAL: 2,
    # ...
}
```

---

### 3. **cabecera_resolver.py**

#### Eliminado import de `resolver_fecha_vencimiento` (línea 35)
```python
# ANTES:
from .field_resolvers import (
    resolver_nif_entidad,
    resolver_nombre_entidad,
    resolver_numero_factura,
    resolver_fecha_expedicion,
    resolver_fecha_vencimiento,  # ← ELIMINADO
    resolver_fecha_operacion,
    _necesita_llm,
    _decidir,
)

# DESPUÉS:
from .field_resolvers import (
    resolver_nif_entidad,
    resolver_nombre_entidad,
    resolver_numero_factura,
    resolver_fecha_expedicion,
    resolver_fecha_operacion,
    _necesita_llm,
    _decidir,
)
```

#### Eliminado bloque completo de `res_fecha_vto` (líneas 234-237)
```python
# ANTES:
res_fecha_vto = resolver_fecha_vencimiento(extractor, self.umbral_auto, self.umbral_warn)
if res_fecha_vto:
    res_fecha_vto = self._aplicar_llm_si_necesario(res_fecha_vto, extractor, raw_document_ai)

# DESPUÉS:
# (bloque completamente eliminado)
```

#### Eliminado `fecha_vencimiento` del constructor `CabeceraResult` (línea 253)
```python
# ANTES:
resultado = CabeceraResult(
    documento_id=doc_id,
    nif_entidad=res_nif.to_dict(),
    nombre_entidad=res_nombre.to_dict(),
    numero_factura=res_num.to_dict(),
    fecha_expedicion=res_fecha_exp.to_dict(),
    fecha_operacion=res_fecha_oper.to_dict(),
    fecha_vencimiento=res_fecha_vto.to_dict() if res_fecha_vto else None,  # ← ELIMINADO
    # ...
).finalizar()

# DESPUÉS:
resultado = CabeceraResult(
    documento_id=doc_id,
    nif_entidad=res_nif.to_dict(),
    nombre_entidad=res_nombre.to_dict(),
    numero_factura=res_num.to_dict(),
    fecha_expedicion=res_fecha_exp.to_dict(),
    fecha_operacion=res_fecha_oper.to_dict(),
    # ...
).finalizar()
```

#### Eliminado `fecha_vencimiento` de `_revalidar_valor()` (línea 380)
```python
# ANTES:
if campo in ("fecha_expedicion", "fecha_vencimiento"):

# DESPUÉS:
if campo == "fecha_expedicion":
```

#### Eliminado keyword de `_extraer_contexto_relevante()` (línea 420)
```python
# ANTES:
_contextos_campo = {
    "nif_entidad": ["cif", "nif", "nie", "tax id", "vat"],
    "nombre_entidad": ["razón social", "empresa", "entidad", "supplier", "from"],
    "numero_factura": ["factura", "invoice", "fra", "nº", "número"],
    "fecha_expedicion": ["fecha", "date", "expedición", "emisión"],
    "fecha_vencimiento": ["vencimiento", "due", "pago", "válido"],  # ← ELIMINADO
}

# DESPUÉS:
_contextos_campo = {
    "nif_entidad": ["cif", "nif", "nie", "tax id", "vat"],
    "nombre_entidad": ["razón social", "empresa", "entidad", "supplier", "from"],
    "numero_factura": ["factura", "invoice", "fra", "nº", "número"],
    "fecha_expedicion": ["fecha", "date", "expedición", "emisión"],
}
```

---

### 4. **test_3_cabecera.py**

#### Corregido path de import (línea 24)
```python
# ANTES:
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "3_identity"))

# DESPUÉS:
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
```

#### Eliminado test `test_fecha_vencimiento_opcional_no_bloquea` (líneas 447-454)
```python
# ANTES:
def test_fecha_vencimiento_opcional_no_bloquea(self):
    # Sin due_date → fecha_vencimiento None, pero no bloquea global
    resultado = self._resolver().resolver(self._raw_completo())
    # ...

# DESPUÉS:
# (test completamente eliminado)
```

---

## Verificación

Todos los checks pasaron exitosamente:

```
[1] OK - SISTEMA in enum
[2] OK - fecha_vencimiento removed from dataclass
[3] OK - resolver_fecha_vencimiento() removed
[4] OK - resolver_fecha_operacion() uses SISTEMA
[5] OK - resolver_fecha_vencimiento removed from imports
[6] OK - _revalidar_valor does not mention fecha_vencimiento
[7] OK - test_fecha_vencimiento_opcional_no_bloquea removed
[8] OK - path corrected in test
[9] OK - to_dict has 4 fields in campos
[10] OK - _CONTEXTO_FECHA_VTO removed
```

---

## Contrato de Salida Final

El JSON de salida de `CabeceraResult.to_dict()` ahora tiene esta estructura:

```json
{
  "documento_id": "...",
  "fase": "3_identidad_cabecera",
  "version_politica": "v1",
  "timestamp": "...",
  "campos": {
    "nif_entidad": { ... },
    "nombre_entidad": { ... },
    "numero_factura": { ... },
    "fecha_expedicion": { ... }
  },
  "decision_global": "auto|warn|block",
  "requiere_revision_humana": true|false,
  "motivos_revision": [...],
  "llm_usado": true|false,
  "tokens_llm": 0
}
```

**IMPORTANTE**:
- Solo 4 campos en `"campos"`: nif_entidad, nombre_entidad, numero_factura, fecha_expedicion
- `fecha_operacion` existe como atributo interno pero NO está en el bloque `"campos"` del JSON
- `fecha_vencimiento` ha sido completamente eliminado del sistema

---

## Archivos Modificados

1. `src/3_identidad_cabecera/field_candidate.py` - Enum + dataclass
2. `src/3_identidad_cabecera/field_resolvers.py` - Función eliminada + fuente cambiada
3. `src/3_identidad_cabecera/cabecera_resolver.py` - Imports + invocación + validación
4. `src/3_identidad_cabecera/test_3_cabecera.py` - Path + test eliminado

---

## Compatibilidad

**Breaking Change**: Sistemas que consumen el JSON de fase 3 y acceden a `fecha_vencimiento` dejarán de funcionar. Actualizar consumidores para usar solo los 4 campos en `"campos"`.
