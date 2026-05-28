"""Tests unitarios del maestro de clientes — tipos_activos."""

from __future__ import annotations

import yaml
from src.phase4_customer.maestro import (
    cargar_maestro, registrar_cliente, guardar_maestro
)


def test_tipos_activos_proveedor(tmp_path):
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "B12345678", "Prov SL", fecha_expedicion="2026-01-15",
                      libro="compras", decision="auto", tipo="proveedor")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert "proveedor" in data["clientes"]["B12345678"]["tipos_activos"]


def test_tipos_activos_cliente(tmp_path):
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "A87654321", "Cliente SA", fecha_expedicion="2026-01-15",
                      libro="ventas", decision="auto", tipo="cliente")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert "cliente" in data["clientes"]["A87654321"]["tipos_activos"]


def test_tipos_activos_no_duplica(tmp_path):
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "B12345678", "Prov SL", fecha_expedicion="2026-01-15",
                      libro="compras", decision="auto", tipo="proveedor")
    registrar_cliente(m, "B12345678", "Prov SL", fecha_expedicion="2026-02-15",
                      libro="compras", decision="auto", tipo="proveedor")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert data["clientes"]["B12345678"]["tipos_activos"].count("proveedor") == 1


def test_tipos_activos_backfill_entry_sin_campo(tmp_path):
    """Existing entries without tipos_activos must get the field on next write."""
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text(
        "clientes:\n"
        "  B12345678:\n"
        "    nombre: Prov SL\n"
        "    documentos_procesados: 1\n"
        "    libros_activos: [compras]\n"
    )
    m = cargar_maestro(str(path))
    registrar_cliente(m, "B12345678", "Prov SL", fecha_expedicion="2026-03-01",
                      libro="compras", decision="auto", tipo="proveedor")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert "tipos_activos" in data["clientes"]["B12345678"]


def test_registrar_cliente_rellena_nombre_si_estaba_vacio(tmp_path):
    """Si la primera confirmación dejó nombre="" (OCR falló y operario no editó),
    la siguiente confirmación con el nombre correcto debe rellenarlo.

    Regresión: registrar_cliente solo seteaba nombre al crear la entrada;
    invocaciones posteriores con un nombre no vacío se ignoraban y el
    historial se quedaba sin nombre para siempre.
    """
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "49915950Q", "", fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    registrar_cliente(m, "49915950Q", "DAVILA PEREZ RECHE",
                      fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert data["clientes"]["49915950Q"]["nombre"] == "DAVILA PEREZ RECHE"


def test_registrar_cliente_no_pisa_nombre_existente_con_vacio(tmp_path):
    """Si la entrada ya tiene un nombre bueno, una confirmación posterior con
    nombre vacío NO debe pisarlo."""
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "49915950Q", "DAVILA PEREZ RECHE",
                      fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    registrar_cliente(m, "49915950Q", "",
                      fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert data["clientes"]["49915950Q"]["nombre"] == "DAVILA PEREZ RECHE"


def test_registrar_cliente_ignora_nombre_solo_espacios(tmp_path):
    """Un nombre con solo whitespace cuenta como vacío para el backfill."""
    path = tmp_path / "maestro_clientes.yaml"
    path.write_text("{}\n")
    m = cargar_maestro(str(path))
    registrar_cliente(m, "49915950Q", "   ", fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    registrar_cliente(m, "49915950Q", "DAVILA PEREZ RECHE",
                      fecha_expedicion="2026-05-19",
                      libro="ventas", decision="auto", tipo="cliente")
    guardar_maestro(str(path), m)
    data = yaml.safe_load(path.read_text())
    assert data["clientes"]["49915950Q"]["nombre"] == "DAVILA PEREZ RECHE"
