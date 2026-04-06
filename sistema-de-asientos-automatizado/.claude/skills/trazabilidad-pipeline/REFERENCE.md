# REFERENCE — Trazabilidad del Pipeline

Este archivo amplía `Skill.md` con schemas completos, tabla de eventos de auditoría, reglas de retención y ejemplos detallados.

---

## Schema del registro de auditoría (schema_v: 1)

Cada línea en el fichero JSONL de auditoría sigue esta estructura:

```json
{
  "schema_v": 1,
  "ts_proceso": "2026-03-26T10:15:32Z",
  "doc_id": "factura_proveedor_marzo",
  "libro": "20_COMPRAS_GASTOS",
  "archivo_origen": "factura_proveedor_marzo.pdf",
  "fases": {
    "ocr":                 {"ok": true,  "motivo": null},
    "identidad_cabecera": {"ok": true,  "motivo": null},
    "fiscal":              {"ok": false, "motivo": "módulo no ejecutado"},
    "ensamblador":         {"ok": true,  "motivo": null}
  },
  "campos_criticos": {
    "nif_entidad":     {"valor": "B12345678", "confianza": 0.99, "decision": "auto"},
    "nombre_entidad":  {"valor": "PROVEEDOR SL", "confianza": 0.97, "decision": "auto"},
    "numero_factura":  {"valor": "F-2026-001", "confianza": 0.99, "decision": "auto"},
    "fecha_expedicion":{"valor": "2026-03-15", "confianza": 0.99, "decision": "auto"},
    "total_euros":     {"valor": 121.00, "confianza": 0.99, "decision": "auto"},
    "nif_receptor":     {"valor": null, "confianza": null, "decision": "pendiente"},
    "concepto":        {"valor": null, "confianza": null, "decision": "pendiente"}
  },
  "decision_global": "pendiente",
  "autocargable": false,
  "motivos_revision": [
    "nif_receptor: Módulo cliente_destino no ejecutado.",
    "concepto: Módulo semantica no ejecutado."
  ],
  "artefactos": {
    "documento_extraido": "data/output/factura_proveedor_marzo/documento_extraido.json",
    "resultado_identidad_cabecera": "data/output/factura_proveedor_marzo/resultado_identidad_cabecera.json",
    "resultado_validacion": "data/output/factura_proveedor_marzo/resultado_validacion.json"
  }
}
```

---

## Qué debe poder demostrarse con este registro

- Qué archivo entró, en qué libro y cuándo
- Qué fases se ejecutaron y cuáles fallaron
- Qué valores se extrajeron de los campos críticos
- Por qué un documento fue a revisión humana o a autocarga
- Dónde están los artefactos para reconstruir el expediente completo

---

## Valores válidos de `decision` por campo

| Valor | Significado |
| --- | --- |
| `auto` | Confianza >= umbral, autocargable |
| `warn` | Confianza por debajo del umbral automático, requiere revisión |
| `block` | Sin candidatos válidos o confianza insuficiente, bloqueado |
| `pendiente` | Módulo que produce este campo aún no ejecutado |

---

## Valores válidos de `decision_global`

`auto` | `warn` | `block` | `pendiente`

Regla: si **cualquier campo crítico** tiene `block`, `decision_global = block`. Si todos son `auto`, `decision_global = auto`. Si hay al menos un `warn` pero ningún `block`, `decision_global = warn`. Si hay al menos un `pendiente` pero ningún `block`/`warn`, `decision_global = pendiente`.

---

## Eventos de auditoría por fase del pipeline

| Evento | Cuándo | Datos mínimos en el registro |
| --- | --- | --- |
| Recepción | Al entrar email o escaneo | `doc_id`, canal, nombre fichero, hash original, fecha/hora |
| Clasificación | Al asignar libro (compras/ventas/bienes) | libro propuesto, regla o usuario, confianza |
| Extracción OCR | Tras Cloud Vision + Gemini | versión del extractor, campos extraídos, hash de salida |
| Validación | Tras reglas fiscales | reglas ejecutadas, resultado, errores, necesidad de revisión |
| Revisión humana | Cada corrección manual | usuario, campos cambiados, antes/después, motivo |
| Propuesta de asiento | Al generar asiento | libro, periodo, claves fiscales, importes, referencia documento |
| Exportación | Al enviar a Intermega | destino, identificador externo, estado, hash payload |
| Rectificación | Si se corrige o anula | asiento afectado, motivo, usuario, evento previo |

---

## Retención y acceso

- **Log técnico (capa 1)**: retención 30-90 días, salvo incidentes abiertos
- **Registro de auditoría (capa 2)**: retención mínima 4 años, alineada con el plazo fiscal de conservación de facturas
- **Acceso al registro de auditoría**: restringido por rol
- **No guardar** en logs: texto libre de emails, adjuntos duplicados, datos personales no necesarios para la auditoría

---

## Ejemplo de log técnico correcto vs incorrecto

### Correcto — nivel INFO sin datos fiscales

```python
logger.info("[identidad] Resuelto NIF doc_id=factura_001 decision=auto confianza=0.99")
logger.info("[ensamblador] Completado doc_id=factura_001 decision_global=auto autocargable=True")
logger.error("[ocr] Fallo al procesar doc_id=factura_001", exc_info=True)
```

### Incorrecto — expone datos fiscales o personales

```python
logger.info(f"[identidad] NIF encontrado: B12345678 para EMPRESA SL")  # expone NIF y nombre
logger.warning(f"[fiscal] Total factura: 1234.56 EUR no cuadra")  # expone importe
logger.error("[ocr] Error procesando factura")  # falta exc_info=True
```

---

## Ejemplo de flujo completo de un documento

```
1. Oficinista deposita factura_marzo.pdf en 20_COMPRAS_GASTOS/
2. Pipeline arranca:
   a. OCR (fase 2) → documento_extraido.json
      Log: [ocr] Iniciando doc_id=factura_marzo
      Log: [ocr] Completado doc_id=factura_marzo campos_extraidos=12

   b. Identidad (fase 3) → resultado_identidad_cabecera.json
      Log: [identidad] Resuelto doc_id=factura_marzo decision=auto

   c. Ensamblador (fase 4) → resultado_ensamblador.json
      Log: [ensamblador] Completado doc_id=factura_marzo decision_global=auto

   d. Audit writer escribe 1 línea en logs/audit/20_COMPRAS_GASTOS_2026-03-26.jsonl
      Log: [audit] Registro escrito doc_id=factura_marzo libro=20_COMPRAS_GASTOS

3. Factura aparece en Excel de autocarga o revisión según decision_global
```
