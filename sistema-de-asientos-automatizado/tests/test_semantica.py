"""
Tests del módulo semántico (fase 3.3).

Verifica:
  - Carga de catálogos
  - Matching por patrones
  - Lookup por NIF proveedor
  - Validación de cuenta en whitelist
  - Coherencia libro/grupo PGC
  - Bienes de inversión → WARN forzado
  - human_review_required → WARN forzado
  - Integración: resolver_semantica produce output correcto
  - Compatibilidad con ensamblador

No requiere credenciales de GCP (LLM mockeado).
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.phase3_semantica.catalogo import (
    _normalizar_texto,
    buscar_por_nif,
    buscar_por_patrones,
    cargar_catalogo_semantica,
    cargar_maestro_proveedores,
)
from src.phase3_semantica.resolver import (
    resolver_semantica,
    _validar_cuenta_en_libro,
    _verificar_coherencia_libro,
    _es_bienes_inversion,
    _cargar_cuentas_validas,
    _cargar_conceptos_con_review,
)


# ──────────────────────────────────────────────────────────
# Fixtures: archivos temporales de catálogos
# ──────────────────────────────────────────────────────────

@pytest.fixture
def catalogo_dir(tmp_path):
    """Crear directorio temporal con catálogos de prueba."""
    # catalogo_semantica.yaml
    catalogo = tmp_path / "catalogo_semantica.yaml"
    catalogo.write_text(
        '- patrones: ["COMPRA MERCADERÍAS", "MERCANCÍAS"]\n'
        '  concepto: "mercaderias"\n'
        '  cuentacontable: "600"\n'
        '  confianza_catalogo: 0.98\n'
        '- patrones: ["ALQUILER", "ARRENDAMIENTO"]\n'
        '  concepto: "alquiler_local"\n'
        '  cuentacontable: "621"\n'
        '  confianza_catalogo: 0.99\n'
        '- patrones: ["SEGURO", "PÓLIZA"]\n'
        '  concepto: "seguro"\n'
        '  cuentacontable: "625"\n'
        '  confianza_catalogo: 0.99\n'
        '- patrones: ["ORDENADOR", "EQUIPO INFORMÁTICO"]\n'
        '  concepto: "equipo_informatico"\n'
        '  cuentacontable: "217"\n'
        '  confianza_catalogo: 0.98\n',
        encoding="utf-8",
    )

    # maestro_proveedores.yaml
    proveedores = tmp_path / "maestro_proveedores.yaml"
    proveedores.write_text(
        'proveedores:\n'
        '  - nif: "B15428303"\n'
        '    nombre: "COMERCIAL BLANCO SL"\n'
        '    concepto_defecto: "Compra de mercaderías"\n'
        '    cuentacontable_defecto: "600"\n'
        '    confianza: 0.97\n'
        '  - nif: "A12345678"\n'
        '    nombre: "SEGUROS MAPFRE"\n'
        '    concepto_defecto: "Seguro"\n'
        '    cuentacontable_defecto: "625"\n'
        '    confianza: 0.95\n',
        encoding="utf-8",
    )

    # maestro_contable_fiscal.yaml (mínimo)
    maestro_contable = tmp_path / "maestro_contable_fiscal.yaml"
    maestro_contable.write_text(
        'enums:\n'
        '  concepto:\n'
        '    allowed_values:\n'
        '      - key: "mercaderias"\n'
        '        human_review_required: false\n'
        '      - key: "alquiler_local"\n'
        '        human_review_required: false\n'
        '      - key: "equipo_informatico"\n'
        '        human_review_required: true\n'
        '      - key: "seguro"\n'
        '        human_review_required: false\n',
        encoding="utf-8",
    )

    # maestro_cuentas.yaml — same non-standard format as production file
    maestro_cuentas = tmp_path / "maestro_cuentas.yaml"
    maestro_cuentas.write_text(
        '# maestro_cuentas.yaml\n'
        'cuentas_validas_compras:\n'
        '  - "600"; "601"; "602"; "621"; "622"; "623"; "624"; "625"\n'
        '  - "626"; "627"; "628"; "629"; "631"; "640"; "649"; "642"\n'
        'cuentas_validas_ventas:\n'
        '  - "700"; "701"; "705"; "706"; "708"; "709"; "750"\n'
        'cuentas_validas_inmovilizado:\n'
        '  - "212"; "213"; "216"; "217"; "218"\n',
        encoding="utf-8",
    )

    return tmp_path


# ──────────────────────────────────────────────────────────
# Tests: normalización de texto
# ──────────────────────────────────────────────────────────

class TestNormalizarTexto:
    def test_mayusculas_y_tildes(self):
        assert _normalizar_texto("compra mercaderías") == "COMPRA MERCADERIAS"

    def test_espacios_multiples(self):
        assert _normalizar_texto("  texto   con   espacios  ") == "TEXTO CON ESPACIOS"

    def test_caracteres_especiales(self):
        result = _normalizar_texto("café à la crème")
        assert "CAFE" in result
        assert "CREME" in result


# ──────────────────────────────────────────────────────────
# Tests: carga de catálogos
# ──────────────────────────────────────────────────────────

class TestCargarCatalogos:
    def test_cargar_catalogo_semantica(self, catalogo_dir):
        catalogo = cargar_catalogo_semantica(catalogo_dir / "catalogo_semantica.yaml")
        assert len(catalogo) == 4
        assert catalogo[0]["concepto"] == "mercaderias"
        assert catalogo[0]["cuentacontable"] == "600"

    def test_cargar_catalogo_no_existe(self, tmp_path):
        catalogo = cargar_catalogo_semantica(tmp_path / "no_existe.yaml")
        assert catalogo == []

    def test_cargar_proveedores(self, catalogo_dir):
        proveedores = cargar_maestro_proveedores(catalogo_dir / "maestro_proveedores.yaml")
        assert "B15428303" in proveedores
        assert proveedores["B15428303"]["cuentacontable_defecto"] == "600"

    def test_cargar_proveedores_no_existe(self, tmp_path):
        proveedores = cargar_maestro_proveedores(tmp_path / "no_existe.yaml")
        assert proveedores == {}


# ──────────────────────────────────────────────────────────
# Tests: búsqueda por NIF
# ──────────────────────────────────────────────────────────

class TestBuscarPorNif:
    def test_nif_encontrado(self, catalogo_dir):
        proveedores = cargar_maestro_proveedores(catalogo_dir / "maestro_proveedores.yaml")
        resultado = buscar_por_nif("B15428303", proveedores)
        assert resultado is not None
        assert resultado["cuentacontable"] == "600"
        assert resultado["fuente"] == "maestro_proveedores"

    def test_nif_no_encontrado(self, catalogo_dir):
        proveedores = cargar_maestro_proveedores(catalogo_dir / "maestro_proveedores.yaml")
        resultado = buscar_por_nif("X99999999", proveedores)
        assert resultado is None

    def test_nif_vacio(self, catalogo_dir):
        proveedores = cargar_maestro_proveedores(catalogo_dir / "maestro_proveedores.yaml")
        resultado = buscar_por_nif("", proveedores)
        assert resultado is None


# ──────────────────────────────────────────────────────────
# Tests: búsqueda por patrones
# ──────────────────────────────────────────────────────────

class TestBuscarPorPatrones:
    def test_match_unico(self, catalogo_dir):
        catalogo = cargar_catalogo_semantica(catalogo_dir / "catalogo_semantica.yaml")
        resultado = buscar_por_patrones("Factura por COMPRA MERCADERÍAS varias", catalogo)
        assert len(resultado) >= 1
        assert resultado[0]["concepto"] == "mercaderias"

    def test_match_multiple(self, catalogo_dir):
        catalogo = cargar_catalogo_semantica(catalogo_dir / "catalogo_semantica.yaml")
        texto = "ALQUILER local + SEGURO anual del local"
        resultado = buscar_por_patrones(texto, catalogo)
        assert len(resultado) >= 2
        conceptos = {r["concepto"] for r in resultado}
        assert "alquiler_local" in conceptos
        assert "seguro" in conceptos

    def test_sin_match(self, catalogo_dir):
        catalogo = cargar_catalogo_semantica(catalogo_dir / "catalogo_semantica.yaml")
        resultado = buscar_por_patrones("Texto totalmente irrelevante xyz", catalogo)
        assert resultado == []

    def test_texto_vacio(self, catalogo_dir):
        catalogo = cargar_catalogo_semantica(catalogo_dir / "catalogo_semantica.yaml")
        resultado = buscar_por_patrones("", catalogo)
        assert resultado == []


# ──────────────────────────────────────────────────────────
# Tests: validación de cuentas
# ──────────────────────────────────────────────────────────

class TestValidacionCuentas:
    def test_cargar_cuentas_validas(self, catalogo_dir):
        cuentas = _cargar_cuentas_validas(str(catalogo_dir / "maestro_cuentas.yaml"))
        assert "600" in cuentas["cuentas_validas_compras"]
        assert "700" in cuentas["cuentas_validas_ventas"]
        assert "217" in cuentas["cuentas_validas_inmovilizado"]

    def test_cuenta_valida_compras(self, catalogo_dir):
        cuentas = _cargar_cuentas_validas(str(catalogo_dir / "maestro_cuentas.yaml"))
        valida, _ = _validar_cuenta_en_libro("600", "20_COMPRAS_GASTOS", cuentas)
        assert valida is True

    def test_cuenta_invalida_compras(self, catalogo_dir):
        cuentas = _cargar_cuentas_validas(str(catalogo_dir / "maestro_cuentas.yaml"))
        valida, motivo = _validar_cuenta_en_libro("700", "20_COMPRAS_GASTOS", cuentas)
        assert valida is False
        assert "700" in motivo

    def test_cuenta_vacia(self, catalogo_dir):
        cuentas = _cargar_cuentas_validas(str(catalogo_dir / "maestro_cuentas.yaml"))
        valida, _ = _validar_cuenta_en_libro(None, "20_COMPRAS_GASTOS", cuentas)
        assert valida is False


# ──────────────────────────────────────────────────────────
# Tests: coherencia libro/grupo PGC
# ──────────────────────────────────────────────────────────

class TestCoherenciaLibro:
    def test_grupo6_en_compras(self):
        coherente, _ = _verificar_coherencia_libro("600", "20_COMPRAS_GASTOS")
        assert coherente is True

    def test_grupo7_en_compras_incoherente(self):
        coherente, motivo = _verificar_coherencia_libro("700", "20_COMPRAS_GASTOS")
        assert coherente is False
        assert "grupo" in motivo.lower()

    def test_grupo6_en_ventas_incoherente(self):
        coherente, _ = _verificar_coherencia_libro("621", "21_VENTAS_INGRESOS")
        assert coherente is False

    def test_grupo2_en_inversion(self):
        coherente, _ = _verificar_coherencia_libro("217", "22_BIENES_INVERSION")
        assert coherente is True


# ──────────────────────────────────────────────────────────
# Tests: bienes de inversión
# ──────────────────────────────────────────────────────────

class TestBienesInversion:
    def test_detecta_bienes_inversion(self):
        assert _es_bienes_inversion("22_BIENES_INVERSION") is True

    def test_no_detecta_compras(self):
        assert _es_bienes_inversion("20_COMPRAS_GASTOS") is False


# ──────────────────────────────────────────────────────────
# Tests: human_review_required
# ──────────────────────────────────────────────────────────

class TestHumanReviewRequired:
    def test_cargar_flags(self, catalogo_dir):
        flags = _cargar_conceptos_con_review(str(catalogo_dir / "maestro_contable_fiscal.yaml"))
        assert flags["equipo_informatico"] is True
        assert flags["mercaderias"] is False


# ──────────────────────────────────────────────────────────
# Tests: resolver_semantica integración
# ──────────────────────────────────────────────────────────

class TestResolverSemantica:
    def test_proveedor_conocido_auto(self, catalogo_dir):
        """Proveedor con NIF en maestro → concepto y cuenta resueltos."""
        resultado = resolver_semantica(
            texto_ocr="Factura de proveedor",
            nif_emisor="B15428303",
            nombre_emisor="COMERCIAL BLANCO SL",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        assert resultado.concepto.valor_final is not None
        assert resultado.cuenta_contable.valor_final == "600"
        assert resultado.concepto.fuente_final == "maestro_proveedores"

    def test_patron_catalogo(self, catalogo_dir):
        """Texto OCR con patrón del catálogo → match."""
        resultado = resolver_semantica(
            texto_ocr="ALQUILER LOCAL MENSUAL - MARZO 2026",
            nif_emisor="X99999999",
            nombre_emisor="DESCONOCIDO",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        assert resultado.concepto.valor_final == "alquiler_local"
        assert resultado.cuenta_contable.valor_final == "621"
        assert resultado.decision_global == "auto"

    def test_sin_match_pendiente(self, catalogo_dir):
        """Sin proveedor ni patrón → pendiente."""
        resultado = resolver_semantica(
            texto_ocr="Servicio totalmente desconocido XYZ",
            nif_emisor="Z99999999",
            nombre_emisor="EMPRESA DESCONOCIDA",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        assert resultado.decision_global in ("pendiente", "warn")
        assert resultado.requiere_revision_humana is True

    def test_bienes_inversion_nunca_auto(self, catalogo_dir):
        """Libro bienes de inversión → WARN mínimo, nunca AUTO."""
        resultado = resolver_semantica(
            texto_ocr="COMPRA EQUIPO INFORMÁTICO SERVIDOR NUEVO",
            nif_emisor="X99999999",
            nombre_emisor="TECH SL",
            libro="22_BIENES_INVERSION",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        assert resultado.decision_global != "auto"
        assert resultado.requiere_revision_humana is True

    def test_human_review_required_warn(self, catalogo_dir):
        """Concepto con human_review_required → WARN mínimo."""
        resultado = resolver_semantica(
            texto_ocr="EQUIPO INFORMÁTICO NUEVO PORTÁTIL",
            nif_emisor="X99999999",
            nombre_emisor="TECH SL",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        # equipo_informatico has human_review_required: true
        assert resultado.concepto.decision != "auto"
        assert resultado.requiere_revision_humana is True

    def test_cuenta_invalida_para_libro(self, catalogo_dir):
        """Cuenta 217 (grupo 2) en libro compras → block/warn."""
        resultado = resolver_semantica(
            texto_ocr="ORDENADOR NUEVO PARA OFICINA",
            nif_emisor="X99999999",
            nombre_emisor="TECH SL",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )
        # cuenta 217 no está en whitelist de compras
        assert resultado.cuenta_contable.decision in ("block", "warn")


# ──────────────────────────────────────────────────────────
# Tests: compatibilidad con ensamblador
# ──────────────────────────────────────────────────────────

class TestCompatibilidadEnsamblador:
    def test_schema_compatible(self, catalogo_dir, tmp_path):
        """resultado_semantica.json tiene schema que el ensamblador puede leer."""
        from src.phase3_semantica.resolver import resolver_semantica

        resultado = resolver_semantica(
            texto_ocr="ALQUILER LOCAL MENSUAL",
            nif_emisor="X99999999",
            nombre_emisor="PROPIETARIO",
            libro="20_COMPRAS_GASTOS",
            catalogo_path=str(catalogo_dir / "catalogo_semantica.yaml"),
            proveedores_path=str(catalogo_dir / "maestro_proveedores.yaml"),
            maestro_contable_path=str(catalogo_dir / "maestro_contable_fiscal.yaml"),
            maestro_cuentas_path=str(catalogo_dir / "maestro_cuentas.yaml"),
            config=None,
        )

        # Simular la serialización como la haría main.py
        resultado_json = {
            "documento_id": "test",
            "fase": "3_semantica",
            "version_politica": "v1",
            "timestamp": "2026-04-04T00:00:00Z",
            "campos": {
                "concepto": {
                    "valor_final": resultado.concepto.valor_final,
                    "fuente_final": resultado.concepto.fuente_final,
                    "confianza_final": resultado.concepto.confianza_final,
                    "decision": resultado.concepto.decision,
                    "motivo": resultado.concepto.motivo,
                    "candidatos": resultado.concepto.candidatos,
                },
                "cuenta_contable": {
                    "valor_final": resultado.cuenta_contable.valor_final,
                    "fuente_final": resultado.cuenta_contable.fuente_final,
                    "confianza_final": resultado.cuenta_contable.confianza_final,
                    "decision": resultado.cuenta_contable.decision,
                    "motivo": resultado.cuenta_contable.motivo,
                    "candidatos": resultado.cuenta_contable.candidatos,
                },
            },
            "decision_global": resultado.decision_global,
            "llm_usado": resultado.llm_usado,
        }

        # Verificar que _extraer_campo_de_modulo puede procesarlo
        from src.phase4_ensamblador.ensamblador import _extraer_campo_de_modulo

        campo_concepto = _extraer_campo_de_modulo(resultado_json, "concepto", "semantica")
        assert campo_concepto["valor_final"] == "alquiler_local"
        assert campo_concepto["fuente_modulo"] == "semantica"
        assert campo_concepto["decision"] == "auto"

        campo_cuenta = _extraer_campo_de_modulo(resultado_json, "cuenta_contable", "semantica")
        assert campo_cuenta["valor_final"] == "621"
        assert campo_cuenta["fuente_modulo"] == "semantica"


# ──────────────────────────────────────────────────────────
# Tests: integración main.py (filesystem)
# ──────────────────────────────────────────────────────────

class TestMainIntegration:
    def test_run_semantica_escribe_artefacto(self, catalogo_dir, tmp_path, monkeypatch):
        """run_semantica escribe resultado_semantica.json correctamente."""
        # Crear artefactos de entrada
        doc_dir = tmp_path / "doc_test"
        doc_dir.mkdir()

        # raw_document_ai.json (texto OCR)
        bridge = {"text": "FACTURA ALQUILER LOCAL MENSUAL MARZO 2026", "pages": [], "entities": []}
        (doc_dir / "raw_document_ai.json").write_text(
            json.dumps(bridge), encoding="utf-8"
        )

        # resultado_identidad_cabecera.json
        identidad = {
            "campos": {
                "nif_entidad": {"valor_final": "X99999999"},
                "nombre_entidad": {"valor_final": "PROPIETARIO SL"},
            }
        }
        (doc_dir / "resultado_identidad_cabecera.json").write_text(
            json.dumps(identidad), encoding="utf-8"
        )

        # Monkeypatch paths de maestros y settings
        monkeypatch.setattr(
            "src.phase3_semantica.main.get_settings",
            lambda: _MockSettings(),
        )
        monkeypatch.setattr(
            "src.phase3_semantica.main.Path",
            lambda p: catalogo_dir / Path(p).name if "maestro" in p or "catalogo" in p else Path(p),
        )

        from src.phase3_semantica.main import run_semantica
        ok = run_semantica("doc_test", str(doc_dir), "20_COMPRAS_GASTOS")

        assert ok is True
        output = doc_dir / "resultado_semantica.json"
        assert output.exists()

        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["fase"] == "3_semantica"
        assert "concepto" in data["campos"]
        assert "cuenta_contable" in data["campos"]
        assert data["decision_global"] in ("auto", "warn", "pendiente", "block")


class _MockSettings:
    """Mock mínimo de Settings para tests sin .env."""
    google_application_credentials = ""
    google_cloud_project_id = ""
    gemini_arbitro_model = ""
    gemini_arbitro_location = ""
    semantica_umbral_catalogo = 0.85
    semantica_umbral_confianza_auto = 0.95
    semantica_umbral_confianza_warn = 0.80
