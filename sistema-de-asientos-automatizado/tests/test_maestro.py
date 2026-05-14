"""Tests unitarios del maestro de clientes — tipos_activos."""

from __future__ import annotations

import yaml
import pytest
from pathlib import Path
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
