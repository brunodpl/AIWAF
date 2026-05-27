"""
Tests para el módulo de resolución de cliente destino (phase4_customer).

Verifica:
- Resolución correcta por libro (compras → receptor, ventas → emisor)
- Match exacto por NIF en maestro
- Cliente nuevo genera WARN
- NIF en block propaga block
- Maestro I/O (carga, registro, guardado)
"""

import tempfile
from pathlib import Path

import yaml

from src.phase4_customer.resolver import resolver_cliente, LIBRO_A_ROL_CLIENTE
from src.phase4_customer.maestro import (
    cargar_maestro,
    buscar_cliente,
    registrar_cliente,
    guardar_maestro,
)


def _campos_identidad(
    nif_receptor="B12345678",
    nombre_receptor="PROVEEDOR SL",
    nif_entidad="A87654321",
    nombre_entidad="CLIENTE SL",
    confianza=0.95,
    decision="auto",
):
    """Helper para construir campos de identidad de prueba."""
    return {
        "nif_receptor": {
            "valor_final": nif_receptor,
            "confianza_final": confianza,
            "decision": decision,
        },
        "nombre_receptor": {
            "valor_final": nombre_receptor,
            "confianza_final": confianza,
            "decision": decision,
        },
        "nif_entidad": {
            "valor_final": nif_entidad,
            "confianza_final": confianza,
            "decision": decision,
        },
        "nombre_entidad": {
            "valor_final": nombre_entidad,
            "confianza_final": confianza,
            "decision": decision,
        },
    }


def _maestro_con_cliente(nif="B12345678", nombre="PROVEEDOR SL"):
    return {
        "clientes": {
            nif: {
                "nombre": nombre,
                "fecha_alta": "2026-01-01",
                "documentos_procesados": 1,
            }
        }
    }


class TestResolverClienteCompras:
    """Resolución para libro 20_COMPRAS_GASTOS (receptor = cliente)."""

    def test_match_exacto_nif(self):
        campos = _campos_identidad()
        maestro = _maestro_con_cliente("B12345678")
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "test_001")
        assert result["decision_global"] == "auto"
        assert result["cliente_info"]["es_nuevo"] is False
        assert result["campos"]["nif_cliente"]["valor_final"] == "B12345678"

    def test_cliente_nuevo_genera_warn(self):
        # NIF desconocido y nombre distinto al del maestro → cliente genuinamente nuevo.
        # El nombre del maestro es "PROVEEDOR SL"; usamos otro para evitar coincidencia
        # por nombre que haría es_nuevo=False (bug stale: nombre_receptor coincidía).
        campos = _campos_identidad(nif_receptor="X99999999", nombre_receptor="OTRO PROVEEDOR SA")
        maestro = _maestro_con_cliente("B12345678")
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "test_002")
        assert result["cliente_info"]["es_nuevo"] is True
        # Clientes nuevos nunca son auto: un cliente desconocido en el maestro
        # debe ir a revisión, nunca autocargar en silencio.
        assert result["decision_global"] == "warn"

    def test_nif_block_propaga_block(self):
        campos = _campos_identidad(decision="block")
        maestro = _maestro_con_cliente()
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "test_003")
        assert result["decision_global"] == "block"

    def test_nif_none_es_block(self):
        campos = _campos_identidad(nif_receptor=None)
        maestro = _maestro_con_cliente()
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "test_004")
        assert result["decision_global"] == "block"


class TestResolverClienteVentas:
    """Resolución para libro 21_VENTAS_INGRESOS (emisor = cliente)."""

    def test_ventas_usa_emisor(self):
        campos = _campos_identidad(nif_entidad="A87654321")
        maestro = _maestro_con_cliente("A87654321")
        result = resolver_cliente(campos, "21_VENTAS_INGRESOS", maestro, "test_005")
        assert result["campos"]["nif_cliente"]["valor_final"] == "A87654321"
        assert result["cliente_info"]["es_nuevo"] is False


class TestSalvaguardaClienteDesconocido:
    """Salvaguarda determinista: un cliente desconocido en el maestro nunca autocarga.

    Caso real (LOLI): factura de Luckia (operadora máquinas recreativas) en VENTAS.
    Gemini etiqueta Luckia como emisor → para VENTAS sería "nuestro asociado".
    Luckia no está en el maestro pero la clienta real (receptor) sí → posible
    inversión emisor/receptor → debe ir a revisión, no autocargar.
    """

    def test_ventas_emisor_desconocido_receptor_conocido_warn_inversion(self):
        # Luckia (emisor, A11111111) NO en maestro; clienta (receptor, B12345678) SÍ.
        campos = _campos_identidad(
            nif_entidad="A11111111",
            nombre_entidad="LUCKIA SA",
            nif_receptor="B12345678",
            nombre_receptor="MARIA CONCEPCION PRECEDO",
        )
        maestro = _maestro_con_cliente("B12345678", "MARIA CONCEPCION PRECEDO")
        result = resolver_cliente(campos, "21_VENTAS_INGRESOS", maestro, "luckia")
        assert result["decision_global"] == "warn"
        assert "inversión" in result["campos"]["nif_cliente"]["motivo"].lower()

    def test_compras_receptor_desconocido_emisor_conocido_warn_inversion(self):
        # Cliente (receptor) desconocido pero el otro rol (emisor) sí está en el maestro.
        campos = _campos_identidad(
            nif_receptor="A11111111",
            nombre_receptor="DESCONOCIDO SL",
            nif_entidad="B12345678",
            nombre_entidad="PROVEEDOR CONOCIDO SL",
        )
        maestro = _maestro_con_cliente("B12345678", "PROVEEDOR CONOCIDO SL")
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "c_inv")
        assert result["decision_global"] == "warn"
        assert "inversión" in result["campos"]["nif_cliente"]["motivo"].lower()

    def test_cliente_nuevo_ambos_roles_desconocidos_es_warn(self):
        # Ni emisor ni receptor están en el maestro → cliente genuinamente nuevo → warn.
        campos = _campos_identidad(
            nif_receptor="X99999999",
            nombre_receptor="OTRO PROVEEDOR SA",
            nif_entidad="Y88888888",
            nombre_entidad="OTRO EMISOR SA",
        )
        maestro = _maestro_con_cliente("B12345678", "PROVEEDOR SL")
        result = resolver_cliente(campos, "20_COMPRAS_GASTOS", maestro, "c_nuevo")
        assert result["decision_global"] == "warn"

    def test_ventas_emisor_conocido_es_auto(self):
        # Sin regresión: si el emisor (cliente en VENTAS) está en el maestro → auto.
        campos = _campos_identidad(nif_entidad="A87654321", nombre_entidad="CLIENTE SL")
        maestro = _maestro_con_cliente("A87654321", "CLIENTE SL")
        result = resolver_cliente(campos, "21_VENTAS_INGRESOS", maestro, "v_ok")
        assert result["decision_global"] == "auto"


class TestResolverClienteLibroDesconocido:
    """Libro desconocido genera block."""

    def test_libro_invalido(self):
        campos = _campos_identidad()
        maestro = _maestro_con_cliente()
        result = resolver_cliente(campos, "99_INVALIDO", maestro, "test_006")
        assert result["decision_global"] == "block"


class TestLibroARolCliente:
    """Verifica el mapeo libro → campos de identidad."""

    def test_compras_usa_receptor(self):
        assert LIBRO_A_ROL_CLIENTE["20_COMPRAS_GASTOS"] == ("nif_receptor", "nombre_receptor")

    def test_ventas_usa_emisor(self):
        assert LIBRO_A_ROL_CLIENTE["21_VENTAS_INGRESOS"] == ("nif_entidad", "nombre_entidad")

    def test_bienes_usa_receptor(self):
        assert LIBRO_A_ROL_CLIENTE["22_BIENES_INVERSION"] == ("nif_receptor", "nombre_receptor")


class TestMaestroIO:
    """Tests para carga y escritura del maestro de clientes."""

    def test_cargar_maestro_inexistente(self):
        maestro = cargar_maestro("/nonexistent/path.yaml")
        assert maestro == {"clientes": {}}

    def test_cargar_maestro_valido(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False, encoding="utf-8") as f:
            yaml.dump({"clientes": {"B12345678": {"nombre": "TEST SL"}}}, f)
            f.flush()
            maestro = cargar_maestro(f.name)
        assert "B12345678" in maestro["clientes"]

    def test_buscar_cliente_existente(self):
        maestro = _maestro_con_cliente("B12345678", "TEST SL")
        result = buscar_cliente(maestro, "B12345678")
        assert result is not None
        assert result["nif"] == "B12345678"

    def test_buscar_cliente_no_existente(self):
        maestro = _maestro_con_cliente("B12345678")
        result = buscar_cliente(maestro, "X99999999")
        assert result is None

    def test_registrar_cliente_nuevo(self):
        maestro = {"clientes": {}}
        registrar_cliente(maestro, "B99999999", "NUEVO SL")
        assert "B99999999" in maestro["clientes"]
        assert maestro["clientes"]["B99999999"]["nombre"] == "NUEVO SL"
        assert maestro["clientes"]["B99999999"]["documentos_procesados"] == 1

    def test_registrar_cliente_existente_incrementa(self):
        maestro = _maestro_con_cliente("B12345678")
        registrar_cliente(maestro, "B12345678", "PROVEEDOR SL")
        assert maestro["clientes"]["B12345678"]["documentos_procesados"] == 2

    def test_guardar_y_cargar_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = f.name
        maestro = _maestro_con_cliente("B12345678", "TEST SL")
        guardar_maestro(path, maestro)
        loaded = cargar_maestro(path)
        assert "B12345678" in loaded["clientes"]
        assert loaded["clientes"]["B12345678"]["nombre"] == "TEST SL"
