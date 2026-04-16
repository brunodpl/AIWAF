"""
Tests del modulo semantico (fase 3.3) — LLM-first.

Verifica:
  - Carga de maestro contable v3
  - Filtrado de cuentas por libro
  - Lookup por NIF proveedor
  - Validacion de cuenta en whitelist
  - Coherencia libro/grupo PGC
  - Bienes de inversion -> WARN forzado
  - human_review flags del maestro v3
  - Integracion: resolver_semantica produce output correcto (sin LLM, sin patrones)
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
    cargar_maestro_contable,
    cuentas_para_libro,
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


MAESTRO_V3_YAML = """\
version: 3
name: maestro_contable_fiscal
scope:
  country: ES
  sector: HORECA

cuentas:
  - code: "600"
    label: "Compras de mercaderias"
    group: "6"
    book: "compras_gastos"
    class: "gasto_deducible_interior"
    descripcion: "Bienes adquiridos para reventa sin transformacion"
    human_review: false
  - code: "621"
    label: "Arrendamientos y canones"
    group: "6"
    book: "compras_gastos"
    class: "gasto_deducible_interior"
    descripcion: "Alquiler de local, maquinaria o equipos"
    human_review: false
  - code: "625"
    label: "Primas de seguros"
    group: "6"
    book: "compras_gastos"
    class: "gasto_no_deducible_iva"
    descripcion: "Seguros vinculados a la actividad. Exentos de IVA"
    human_review: false
  - code: "640"
    label: "Sueldos y salarios"
    group: "6"
    book: "compras_gastos"
    class: "gasto_no_deducible_iva"
    descripcion: "Nominas y retribuciones del personal"
    human_review: true
  - code: "217"
    label: "Equipos para procesos de informacion"
    group: "2"
    book: "bienes_inversion"
    class: "bien_inversion"
    descripcion: "Ordenadores, portatiles, TPV, tablets"
    human_review: true
  - code: "700"
    label: "Ventas de mercaderias"
    group: "7"
    book: "ingresos_ventas"
    class: "ingreso_interior"
    descripcion: "Ingresos por venta de productos"
    human_review: false

conceptos:
  mercaderias:         { cuenta: "600", class: "gasto_deducible_interior",  human_review: false }
  alquiler_local:      { cuenta: "621", class: "gasto_deducible_interior",  human_review: false }
  seguro:              { cuenta: "625", class: "gasto_no_deducible_iva",    human_review: false }
  nomina:              { cuenta: "640", class: "gasto_no_deducible_iva",    human_review: true  }
  equipo_informatico:  { cuenta: "217", class: "bien_inversion",            human_review: true  }
  venta_mercaderias:   { cuenta: "700", class: "ingreso_interior",          human_review: false }
  no_clasificado:      { cuenta: null,  class: "pendiente_revision",        human_review: true  }

clases_fiscales:
  gasto_deducible_interior: { book: "compras_gastos",   iva_expected: true  }
  gasto_no_deducible_iva:   { book: "compras_gastos",   iva_expected: false }
  bien_inversion:           { book: "bienes_inversion", iva_expected: true  }
  ingreso_interior:         { book: "ingresos_ventas",  iva_expected: true  }
  pendiente_revision:       { book: null,               iva_expected: null  }
"""


@pytest.fixture
def catalogo_dir(tmp_path):
    """Crear directorio temporal con catalogos de prueba (maestro v3)."""
    # maestro_contable_fiscal.yaml (v3)
    maestro_contable = tmp_path / "maestro_contable_fiscal.yaml"
    maestro_contable.write_text(MAESTRO_V3_YAML, encoding="utf-8")

    # maestro_proveedores.yaml
    proveedores = tmp_path / "maestro_proveedores.yaml"
    proveedores.write_text(
        'proveedores:\n'
        '  - nif: "B15428303"\n'
        '    nombre: "COMERCIAL BLANCO SL"\n'
        '    concepto_defecto: "mercaderias"\n'
        '    cuentacontable_defecto: "600"\n'
        '    confianza: 0.97\n'
        '  - nif: "A12345678"\n'
        '    nombre: "SEGUROS MAPFRE"\n'
        '    concepto_defecto: "seguro"\n'
        '    cuentacontable_defecto: "625"\n'
        '    confianza: 0.95\n',
        encoding="utf-8",
    )

    # maestro_cuentas.yaml
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
# Tests: normalizacion de texto
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
# Tests: carga del maestro contable v3
# ──────────────────────────────────────────────────────────

class TestCargarMaestroContable:
    def test_carga_version_3(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        assert maestro["version"] == 3
        assert len(maestro["cuentas"]) == 6  # solo las del fixture

    def test_cuentas_tienen_campos_requeridos(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        for cuenta in maestro["cuentas"]:
            assert "code" in cuenta
            assert "label" in cuenta
            assert "book" in cuenta
            assert "descripcion" in cuenta
            assert "human_review" in cuenta

    def test_conceptos_tienen_human_review(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        assert maestro["conceptos"]["nomina"]["human_review"] is True
        assert maestro["conceptos"]["mercaderias"]["human_review"] is False

    def test_archivo_no_existe(self, tmp_path):
        maestro = cargar_maestro_contable(tmp_path / "no_existe.yaml")
        assert maestro == {}

    def test_cuentas_para_libro_compras(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        cuentas = cuentas_para_libro(maestro, "20_COMPRAS_GASTOS")
        assert len(cuentas) == 4  # 600, 621, 625, 640
        codigos = [c["code"] for c in cuentas]
        assert "600" in codigos
        assert "621" in codigos
        assert "217" not in codigos  # bienes_inversion, no compras

    def test_cuentas_para_libro_inversion(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        cuentas = cuentas_para_libro(maestro, "22_BIENES_INVERSION")
        codigos = [c["code"] for c in cuentas]
        assert "217" in codigos
        assert "600" not in codigos

    def test_cuentas_para_libro_desconocido(self, catalogo_dir):
        maestro = cargar_maestro_contable(catalogo_dir / "maestro_contable_fiscal.yaml")
        cuentas = cuentas_para_libro(maestro, "99_LIBRO_INEXISTENTE")
        assert cuentas == []


# ──────────────────────────────────────────────────────────
# Tests: busqueda por NIF
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
# Tests: validacion de cuentas
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
# Tests: bienes de inversion
# ──────────────────────────────────────────────────────────

class TestBienesInversion:
    def test_detecta_bienes_inversion(self):
        assert _es_bienes_inversion("22_BIENES_INVERSION") is True

    def test_no_detecta_compras(self):
        assert _es_bienes_inversion("20_COMPRAS_GASTOS") is False


# ──────────────────────────────────────────────────────────
# Tests: human_review flags del maestro v3
# ──────────────────────────────────────────────────────────

class TestHumanReviewRequired:
    def test_cargar_flags(self, catalogo_dir):
        flags = _cargar_conceptos_con_review(str(catalogo_dir / "maestro_contable_fiscal.yaml"))
        assert flags["equipo_informatico"] is True
        assert flags["mercaderias"] is False


# ──────────────────────────────────────────────────────────
# Tests: resolver_semantica integracion
# ──────────────────────────────────────────────────────────

class TestResolverSemantica:
    def test_proveedor_conocido_auto(self, catalogo_dir):
        """Proveedor con NIF en maestro -> concepto y cuenta resueltos."""
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
        """Texto OCR con patron del catalogo -> match."""
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
        """Sin proveedor ni patron -> pendiente."""
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
        """Libro bienes de inversion -> WARN minimo, nunca AUTO."""
        resultado = resolver_semantica(
            texto_ocr="COMPRA EQUIPO INFORMATICO SERVIDOR NUEVO",
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
        """Concepto con human_review_required -> WARN minimo."""
        resultado = resolver_semantica(
            texto_ocr="EQUIPO INFORMATICO NUEVO PORTATIL",
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
        """Cuenta 217 (grupo 2) en libro compras -> block/warn."""
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
        # cuenta 217 no esta en whitelist de compras
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

        # Simular la serializacion como la haria main.py
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
# Tests: integracion main.py (filesystem)
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
    """Mock minimo de Settings para tests sin .env."""
    google_application_credentials = ""
    google_cloud_project_id = ""
    gemini_arbitro_model = ""
    gemini_arbitro_location = ""
    semantica_umbral_catalogo = 0.85
    semantica_umbral_confianza_auto = 0.95
    semantica_umbral_confianza_warn = 0.80
