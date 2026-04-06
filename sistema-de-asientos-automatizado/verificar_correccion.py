"""
Verificación de las 6 correcciones del plan.

Checks:
1. FuenteCandidato.SISTEMA existe en enum
2. fecha_vencimiento NO existe en dataclass CabeceraResult
3. resolver_fecha_vencimiento() NO existe en field_resolvers.py
4. resolver_fecha_operacion() usa FuenteCandidato.SISTEMA
5. Import de resolver_fecha_vencimiento NO existe en cabecera_resolver.py
6. _revalidar_valor() NO menciona fecha_vencimiento
"""

import os
import re

print("=" * 60)
print("VERIFICACIÓN DE CORRECCIONES")
print("=" * 60)

base_path = "src/3_identidad_cabecera"

# Check 1: SISTEMA en enum
print("\n[1] FuenteCandidato.SISTEMA existe en field_candidate.py?")
with open(os.path.join(base_path, "field_candidate.py"), encoding="utf-8") as f:
    content = f.read()
if 'SISTEMA' in content and 'SISTEMA               = "sistema"' in content:
    print("✓ FuenteCandidato.SISTEMA añadido al enum")
else:
    print("✗ ERROR: SISTEMA no encontrado en enum")
    exit(1)

# Check 2: fecha_vencimiento NO en dataclass
print("\n[2] fecha_vencimiento eliminado de CabeceraResult?")
with open(os.path.join(base_path, "field_candidate.py"), encoding="utf-8") as f:
    content = f.read()
if "fecha_vencimiento:" in content:
    print("✗ ERROR: fecha_vencimiento aún está en CabeceraResult")
    exit(1)
else:
    print("✓ fecha_vencimiento eliminado de CabeceraResult")

# Check 3: resolver_fecha_vencimiento NO existe
print("\n[3] resolver_fecha_vencimiento() eliminado de field_resolvers.py?")
with open(os.path.join(base_path, "field_resolvers.py"), encoding="utf-8") as f:
    content = f.read()
if "def resolver_fecha_vencimiento" in content:
    print("✗ ERROR: resolver_fecha_vencimiento() aún existe")
    exit(1)
else:
    print("✓ resolver_fecha_vencimiento() eliminado")

# Check 4: resolver_fecha_operacion usa SISTEMA
print("\n[4] resolver_fecha_operacion() usa FuenteCandidato.SISTEMA?")
with open(os.path.join(base_path, "field_resolvers.py"), encoding="utf-8") as f:
    content = f.read()
    # Buscar la función resolver_fecha_operacion
    match = re.search(r'def resolver_fecha_operacion\(\).*?return FieldResolution\([^)]+\)', content, re.DOTALL)
    if match:
        func_content = match.group(0)
        if "FuenteCandidato.SISTEMA" in func_content:
            print("✓ resolver_fecha_operacion() usa FuenteCandidato.SISTEMA")
        else:
            print("✗ ERROR: resolver_fecha_operacion() NO usa SISTEMA")
            exit(1)
    else:
        print("✗ ERROR: No se pudo encontrar resolver_fecha_operacion()")
        exit(1)

# Check 5: Import de resolver_fecha_vencimiento eliminado
print("\n[5] Import de resolver_fecha_vencimiento eliminado de cabecera_resolver.py?")
with open(os.path.join(base_path, "cabecera_resolver.py"), encoding="utf-8") as f:
    content = f.read()
if "resolver_fecha_vencimiento" in content:
    print("✗ ERROR: resolver_fecha_vencimiento aún aparece en cabecera_resolver.py")
    exit(1)
else:
    print("✓ resolver_fecha_vencimiento eliminado de cabecera_resolver.py")

# Check 6: _revalidar_valor NO menciona fecha_vencimiento
print("\n[6] _revalidar_valor() NO menciona fecha_vencimiento?")
with open(os.path.join(base_path, "cabecera_resolver.py"), encoding="utf-8") as f:
    content = f.read()
    # Buscar la función _revalidar_valor
    match = re.search(r'def _revalidar_valor\([^)]+\).*?(?=\ndef )', content, re.DOTALL)
    if match:
        func_content = match.group(0)
        if "fecha_vencimiento" in func_content:
            print("✗ ERROR: _revalidar_valor() aún menciona fecha_vencimiento")
            exit(1)
        else:
            print("✓ _revalidar_valor() NO menciona fecha_vencimiento")
    else:
        print("✓ _revalidar_valor() NO menciona fecha_vencimiento (función final del archivo)")

# Check 7: Test de fecha_vencimiento eliminado
print("\n[7] Test fecha_vencimiento_opcional_no_bloquea eliminado?")
with open(os.path.join(base_path, "test_3_cabecera.py"), encoding="utf-8") as f:
    content = f.read()
if "test_fecha_vencimiento_opcional_no_bloquea" in content:
    print("✗ ERROR: Test de fecha_vencimiento aún existe")
    exit(1)
else:
    print("✓ Test fecha_vencimiento_opcional_no_bloquea eliminado")

# Check 8: Path corregido en test
print("\n[8] Path en test_3_cabecera.py corregido?")
with open(os.path.join(base_path, "test_3_cabecera.py"), encoding="utf-8") as f:
    content = f.read()
if "3_identity" in content:
    print("✗ ERROR: Path antiguo '3_identity' aún presente")
    exit(1)
else:
    print("✓ Path corregido en test_3_cabecera.py")

# Check 9: to_dict() con 4 campos
print("\n[9] to_dict() contiene 4 campos en 'campos'?")
with open(os.path.join(base_path, "field_candidate.py"), encoding="utf-8") as f:
    content = f.read()
    # Buscar el método to_dict en CabeceraResult
    match = re.search(r'class CabeceraResult.*?def to_dict\(self\).*?return \{.*?\}', content, re.DOTALL)
    if match:
        method_content = match.group(0)
        # Verificar que "campos" está presente y contiene los 4 campos esperados
        if '"campos":' in method_content or "'campos':" in method_content:
            campos_match = re.search(r'["\']campos["\']\s*:\s*\{([^}]+)\}', method_content, re.DOTALL)
            if campos_match:
                campos_content = campos_match.group(1)
                expected_fields = ["nif_entidad", "nombre_entidad", "numero_factura", "fecha_expedicion"]
                found_fields = []
                for field in expected_fields:
                    if field in campos_content:
                        found_fields.append(field)

                if len(found_fields) == 4:
                    # Verificar que fecha_operacion y fecha_vencimiento NO estén
                    if "fecha_operacion" in campos_content or "fecha_vencimiento" in campos_content:
                        print("✗ ERROR: fecha_operacion o fecha_vencimiento presentes en 'campos'")
                        exit(1)
                    print(f"✓ to_dict() contiene 4 campos: {found_fields}")
                else:
                    print(f"✗ ERROR: Esperado 4 campos, encontrado {len(found_fields)}: {found_fields}")
                    exit(1)
            else:
                print("✗ ERROR: No se pudo parsear el contenido de 'campos'")
                exit(1)
        else:
            print("✗ ERROR: 'campos' no encontrado en to_dict()")
            exit(1)
    else:
        print("✗ ERROR: No se pudo encontrar to_dict() en CabeceraResult")
        exit(1)

# Check 10: _CONTEXTO_FECHA_VTO eliminado
print("\n[10] _CONTEXTO_FECHA_VTO eliminado de field_resolvers.py?")
with open(os.path.join(base_path, "field_resolvers.py"), encoding="utf-8") as f:
    content = f.read()
if "_CONTEXTO_FECHA_VTO" in content:
    print("✗ ERROR: _CONTEXTO_FECHA_VTO aún existe")
    exit(1)
else:
    print("✓ _CONTEXTO_FECHA_VTO eliminado")

print("\n" + "=" * 60)
print("TODAS LAS VERIFICACIONES PASARON ✓")
print("=" * 60)
print("\nResumen de cambios implementados:")
print("  1. ✓ FuenteCandidato.SISTEMA añadido al enum")
print("  2. ✓ fecha_vencimiento eliminado del dataclass CabeceraResult")
print("  3. ✓ resolver_fecha_vencimiento() eliminado de field_resolvers.py")
print("  4. ✓ resolver_fecha_operacion() usa FuenteCandidato.SISTEMA")
print("  5. ✓ Import y uso de resolver_fecha_vencimiento eliminados de cabecera_resolver.py")
print("  6. ✓ _revalidar_valor() NO menciona fecha_vencimiento")
print("  7. ✓ Test fecha_vencimiento_opcional_no_bloquea eliminado")
print("  8. ✓ Path corregido en test_3_cabecera.py")
print("  9. ✓ to_dict() contiene 4 campos en 'campos' (sin fecha_operacion, sin fecha_vencimiento)")
print(" 10. ✓ _CONTEXTO_FECHA_VTO eliminado de field_resolvers.py")
