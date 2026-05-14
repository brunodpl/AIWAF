"""Tests del schema ampliado de maestro_clientes.yaml (v0.4)."""

from __future__ import annotations

from src.phase4_customer.maestro import registrar_cliente


def test_registrar_cliente_actualiza_ultima_factura_y_libros():
    maestro: dict = {}
    registrar_cliente(
        maestro,
        "B15234567",
        "Cabreiroá, S.L.",
        fecha_expedicion="2026-04-21",
        libro="compras",
        decision="auto",
    )
    c = maestro["clientes"]["B15234567"]
    assert c["ultima_factura_fecha"] == "2026-04-21"
    assert c["libros_activos"] == ["compras"]
    assert c["documentos_procesados"] == 1


def test_registrar_cliente_no_retrocede_ultima_factura():
    maestro: dict = {}
    registrar_cliente(maestro, "B15234567", "Cabreiroá", "2026-04-21", "compras", "auto")
    registrar_cliente(maestro, "B15234567", "Cabreiroá", "2026-03-01", "ventas", "auto")
    c = maestro["clientes"]["B15234567"]
    assert c["ultima_factura_fecha"] == "2026-04-21"
    assert set(c["libros_activos"]) == {"compras", "ventas"}
    assert c["documentos_procesados"] == 2


def test_registrar_cliente_block_no_incrementa():
    maestro: dict = {}
    registrar_cliente(maestro, "B15234567", "X", "2026-04-21", "compras", "block")
    assert maestro.get("clientes", {}).get("B15234567") is None


def test_registrar_cliente_warn_si_se_incluye():
    """Decisiones warn también registran al cliente (revisado por operario)."""
    maestro: dict = {}
    registrar_cliente(maestro, "B15234567", "X", "2026-04-21", "compras", "warn")
    assert maestro["clientes"]["B15234567"]["documentos_procesados"] == 1
    assert maestro["clientes"]["B15234567"]["libros_activos"] == ["compras"]


def test_registrar_cliente_libro_duplicado_no_duplica():
    maestro: dict = {}
    registrar_cliente(maestro, "B15234567", "X", "2026-04-21", "compras", "auto")
    registrar_cliente(maestro, "B15234567", "X", "2026-04-22", "compras", "auto")
    assert maestro["clientes"]["B15234567"]["libros_activos"] == ["compras"]


def test_registrar_cliente_compat_firma_antigua():
    """Compatibilidad con call sites legacy: registrar_cliente(m, nif, nombre)."""
    maestro: dict = {}
    registrar_cliente(maestro, "B15234567", "Cabreiroá")
    c = maestro["clientes"]["B15234567"]
    assert c["nombre"] == "Cabreiroá"
    assert c["documentos_procesados"] == 1
    assert c["ultima_factura_fecha"] is None
    assert c["libros_activos"] == []
