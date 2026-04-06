---
name: trazabilidad-pipeline
description: Framework de trazabilidad del sistema de automatización de facturas para gestoría española. Usar cuando se implemente cualquier módulo nuevo, fase del pipeline, se modifique lógica que procese documentos fiscales, se configure logging, se escriban registros de auditoría, o se toque cualquier parte del sistema de logs y trazabilidad de datos. También usar cuando se revise cumplimiento RGPD en logs, se diseñen artefactos de salida por documento, o se necesite entender la separación entre log técnico y registro de auditoría. Si estás tocando logging o auditoría en cualquier fase, usa esta skill.
---

# Trazabilidad del Pipeline de Facturas

## Propósito

Esta skill define el framework de trazabilidad del sistema de automatización de facturas. Úsala cuando implementes cualquier módulo nuevo, fase del pipeline, o modifiques lógica existente que procese documentos fiscales.

Esta skill **no cubre normativa fiscal** (existe skill dedicada: `/normativa-facturas-espana`). Cubre exclusivamente: arquitectura de logging, estructura de auditoría, reglas de implementación y patrones correctos por fase.

## Principio fundamental

El pipeline tiene **dos capas de registro completamente separadas**. Confundirlas es el error más frecuente.

| Capa | Nombre | Audiencia | Formato | Destino |
| --- | --- | --- | --- | --- |
| 1 | Log técnico | Desarrollador, debugging | Texto plano | `stdout` / `logs/pipeline.jsonl` (rotativo) |
| 2 | Registro de auditoría | Gestoría, cliente, inspección fiscal | JSON estructurado | `logs/audit/{libro}_{fecha}.jsonl` (append-only) |

**Regla**: el log técnico (capa 1) no tiene valor legal. El registro de auditoría (capa 2) sí. Nunca mezcles ambos.

---

## Logging técnico (Capa 1) — Reglas obligatorias

### Un solo punto de configuración

```python
# src/logging_config.py — UNICO lugar donde se configuran handlers
def setup_logging(logs_path: str, level: int = logging.INFO) -> None:
    # stdout handler + FileHandler rotativo
    # Solo se llama desde entry points (main() o pipeline.py)
    ...
```

### Reglas que TODOS los módulos deben cumplir

1. **Solo `logging.getLogger(nombre)`** — nunca `logging.basicConfig()`, nunca `addHandler()`, nunca `setLevel()` en módulos.
2. **Logger a nivel de módulo**, no dentro de funciones:

```python
# CORRECTO — nivel modulo
logger = logging.getLogger("pipeline.ocr")

# INCORRECTO — dentro de funcion, reinstancia cada llamada
def procesar():
    logger = logging.getLogger("pipeline.ocr")
```

3. **`setup_logging()` solo desde entry points** (`main()` de CLIs standalone o `pipeline.py`). Nunca desde `run_*()` ni módulos internos.
4. **Sin wrappers** (`get_logger()`, `logger.py` custom, etc.) — solo `logging.getLogger` estándar de Python.
5. **`exc_info=True`** en todos los `logger.error()` para capturar stack trace completo.

### Jerarquía de loggers

```
root (configurado por setup_logging)
└── pipeline
    ├── pipeline.ocr                  <- phase2_ocr/* (Cloud Vision + Gemini)
    ├── pipeline.identidad            <- phase3_identity/*
    ├── pipeline.ensamblador          <- phase4_assemble/*
    └── pipeline.audit                <- audit_writer.py
```

Todos propagan al root. **No añadir handlers en nodos intermedios.**

### Prefijos obligatorios en mensajes

Cada módulo prefija sus mensajes con su fase entre corchetes:

```python
# Fase 2: OCR
logger.info("[Vision] Procesando archivo.pdf (2 páginas)")
logger.info("[Gemini] JSON recibido — 8 campos, 2 líneas fiscales")
logger.warning("[Gemini] Suma fiscal NO cuadra → requiere_revision=True")

# Fase 3: Resolución
logger.info("[identidad] Artefacto escrito decision_global=auto")
logger.warning("[identidad] Campo nif_entidad no resuelto doc_id=factura_001")

# Pipeline general
logger.info("[pipeline] OCR completado: factura_001 (valid=True)")
```

### Regla RGPD en mensajes de log

- `DEBUG`: puede incluir valores de campos (solo en desarrollo)
- `INFO`: doc_id, decisiones, rutas, conteos — **nunca valores fiscales**
- `WARNING` y superiores: solo nombre del campo y decisión, **nunca el valor extraído**

```python
# CORRECTO
logger.warning("[identidad] validacion: identificacion.nif_entidad ausente doc_id=factura_001")

# INCORRECTO — expone dato fiscal en log tecnico
logger.warning(f"[identidad] NIF invalido: B1234567X doc_id=factura_001")

# INCORRECTO — expone importes en WARNING
logger.warning(f"[Gemini] Suma fiscal: 159.04 vs 159.00")
```

---

## Registro de auditoría (Capa 2) — Resumen

Un fichero JSONL por ejecución de carpeta en `logs/audit/`. Append-only. Una línea JSON por documento. Lo escribe **exclusivamente `audit_writer.py`**.

```
logs/
├── pipeline.jsonl              <- log tecnico rotativo (capa 1)
└── audit/
    ├── 20_COMPRAS_GASTOS_2026-03-26.jsonl
    └── 21_VENTAS_INGRESOS_2026-03-26.jsonl
```

Para el schema completo del registro de auditoría, valores válidos de `decision` y `decision_global`, y la tabla de eventos por fase, consulta `REFERENCE.md`.

---

## Configuración compartida

`src/config.py` es el Settings global del pipeline. **No crear `config.py` por fase** salvo configuración que no pueda vivir en el Settings global. `phase2_ocr/config.py` existe por compatibilidad y redirige a `src/config.py`.

```python
# Campos relevantes para trazabilidad
logs_path: str     # default: "logs"        -> raiz de logs tecnicos
output_path: str   # default: "data/output" -> raiz de artefactos por documento
```

Cualquier módulo que necesite configuración:

```python
from src.config import get_settings
```

---

## Estructura de artefactos por documento

```
data/output/
└── {documento_id}/
    ├── documento_extraido.json           <- campos mapeados con confianza (fase 2: Cloud Vision + Gemini)
    ├── resultado_identidad_cabecera.json <- resolución NIF, nombre, fecha, n factura (fase 3)
    ├── resultado_validacion.json         <- validación fiscal (fase futura)
    └── resultado_ensamblador.json        <- output final consolidado (fase 4)
```

`documento_id` = basename del archivo original sin extensión. **Sin UUID, estable y predecible** para auditoría.

Nota: con la migración a Cloud Vision + Gemini, ya no se genera `raw_document_ai.json`. El texto plano extraído por Vision se pasa directamente a Gemini para estructuración; no se persiste como artefacto intermedio.

---

## Trazabilidad por fase

### Fase 2 (OCR ligero)

Fase 2 produce `documento_extraido.json` con confianzas asignadas determinísticamente (máximo 0.95). Es un punto de partida, no una resolución definitiva. La trazabilidad de esta fase se limita a:
- Log de caracteres extraídos por Vision
- Log de campos y líneas devueltos por Gemini
- Si Gemini falla: log del fallback a `json_minimos`
- `origen.ocr_engine` en el JSON registra `"cloud_vision+gemini-2.5-flash"`

### Fase 3 (Resolución de campos)

Fase 3 es donde se resuelven los campos definitivamente. Cada módulo de fase 3 debe registrar en auditoría:
- Qué campos modificó respecto a fase 2
- Qué confianza asignó y por qué
- Si usó LLM (árbitro) y con qué resultado
- Decisión por campo: `auto`, `warn`, `pendiente`, `block`

### Fase 4 (Ensamblado)

El ensamblador consolida todas las decisiones de fase 3 y produce `resultado_ensamblador.json`. Registra la `decision_global` final.

---

## Checklist al implementar una fase nueva

Antes de hacer commit de cualquier módulo nuevo:

- [ ] `logger = logging.getLogger("pipeline.{fase}")` a nivel de módulo
- [ ] Sin `addHandler()`, sin `setLevel()`, sin `basicConfig()` en el módulo
- [ ] Si es entry point CLI: `main()` llama `setup_logging()` como primera instrucción
- [ ] `exc_info=True` en todos los `logger.error()`
- [ ] Mensajes con prefijo `[fase]` y `doc_id=` cuando aplique
- [ ] WARNING y superiores sin valores de campos fiscales (RGPD)
- [ ] Si la fase produce campos críticos: `audit_writer.py` actualizado con esos campos
- [ ] Artefacto de salida referenciado en `artefactos{}` del registro de auditoría

---

## Riesgos conocidos

1. **Log != libro registro**: trazabilidad perfecta en el pipeline no reemplaza la obligación de conservar facturas originales ni los libros exigidos por la normativa del IVA.
2. **Sobrelogging**: loggear contenido documental innecesario complica operación, coste y cumplimiento RGPD.
3. **Expansión de alcance**: si el sistema pasa de registrar asientos a emitir facturas, hay que revisar encaje con normativa técnica de sistemas de facturación.
4. **Sin raw intermedio**: al no persistir el texto plano de Vision como artefacto, no hay forma de re-procesar solo la fase Gemini sin volver a llamar a Vision. Esto es aceptable para el volumen actual pero podría revisarse si se necesita reprocesamiento parcial.
