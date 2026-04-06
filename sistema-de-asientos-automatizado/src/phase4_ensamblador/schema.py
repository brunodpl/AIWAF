"""
Registros declarativos del ensamblador.

Pura configuración, cero lógica. Añadir un módulo nuevo:
  1. Añadir entrada en MODULOS_FASE3
  2. Añadir sus campos en CAMPOS_POR_MODULO
  3. Marcar como obligatorio en CAMPOS_OBLIGATORIOS si aplica
  Zero cambios en ensamblador.py
"""

# Artefactos JSON esperados por módulo pre-ensamblador (fase 3 + fase 4).
# El nombre del dict es histórico; incluye módulos de ambas fases.
MODULOS_FASE3: dict[str, str] = {
    "identidad_cabecera": "resultado_identidad_cabecera.json",
    "fiscal":             "resultado_fiscal.json",
    "semantica":          "resultado_semantica.json",
    "cliente_destino":    "resultado_cliente.json",
}

# Artefacto del OCR (fase 2) — siempre presente si el pipeline llegó aquí
ARTEFACTO_OCR = "documento_extraido.json"

# Qué módulo es responsable de cada campo del asiento final
CAMPOS_POR_MODULO: dict[str, str] = {
    "nif_entidad":      "identidad_cabecera",
    "nombre_entidad":   "identidad_cabecera",
    "numero_factura":   "identidad_cabecera",
    "fecha_expedicion": "identidad_cabecera",
    "fecha_operacion":  "identidad_cabecera",
    "total_euros":      "fiscal",
    "lineas_fiscales":  "fiscal",
    "concepto":         "semantica",
    "cuenta_contable":  "semantica",
    "nif_receptor":      "identidad_cabecera",
    "nombre_receptor":   "identidad_cabecera",
    "nif_cliente":       "cliente_destino",
    "nombre_cliente":    "cliente_destino",
}

# Campos que bloquean autocarga si no están en AUTO
CAMPOS_OBLIGATORIOS: frozenset[str] = frozenset({
    "nif_entidad",
    "nombre_entidad",
    "numero_factura",
    "fecha_expedicion",
    "total_euros",
    "nif_receptor",
    "concepto",
    "nif_cliente",
    "nombre_cliente",
})

# Plantilla para campo sin módulo ejecutado o con error técnico
# El ensamblador sustituye {modulo} antes de usar.
CAMPO_PENDIENTE: dict = {
    "valor_final":   None,
    "fuente_modulo": "pendiente",
    "fuente_dato":   None,
    "confianza":     None,
    "decision":      "pendiente",
    "llm_usado":     False,
    "motivo":        "Módulo {modulo} no ejecutado.",
    "ocr_fallback":  False,
}

CAMPO_ERROR: dict = {
    "valor_final":   None,
    "fuente_modulo": "error",
    "fuente_dato":   None,
    "confianza":     None,
    "decision":      "block",
    "llm_usado":     False,
    "motivo":        "Módulo {modulo} falló con error técnico.",
    "ocr_fallback":  False,
}
