"""Tests del file-lock cross-process en guardar_maestro()."""

from __future__ import annotations

import threading

from src.phase4_customer.maestro import (
    cargar_maestro,
    guardar_maestro,
    registrar_cliente,
)


def test_dos_threads_no_pierden_clientes(tmp_path):
    """
    Escenario: dos threads, cada uno carga el maestro, añade un NIF
    distinto y guarda. Sin merge bajo lock, el segundo pisa al primero.
    Con merge bajo lock, los dos NIFs sobreviven.
    """
    yaml_path = str(tmp_path / "maestro.yaml")

    barrier = threading.Barrier(2)

    def worker(nif: str) -> None:
        m = cargar_maestro(yaml_path)
        registrar_cliente(
            m,
            nif,
            f"Cliente {nif}",
            fecha_expedicion="2026-04-21",
            libro="compras",
            decision="auto",
        )
        # Forzar contención: ambos threads esperan antes de escribir.
        barrier.wait(timeout=5)
        guardar_maestro(yaml_path, m)

    t1 = threading.Thread(target=worker, args=("B11111111",))
    t2 = threading.Thread(target=worker, args=("B22222222",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    final = cargar_maestro(yaml_path)
    clientes = final["clientes"]
    assert "B11111111" in clientes, f"perdido B1, contenido={list(clientes)}"
    assert "B22222222" in clientes, f"perdido B2, contenido={list(clientes)}"


def test_merge_preserva_documentos_procesados_acumulados(tmp_path):
    """
    Si el disco tiene NIF X con documentos_procesados=5 y memoria también
    tiene X con documentos_procesados=3 (porque se cargó antes de un
    incremento concurrente), el merge debe conservar max(disk, mem) y
    fusionar listas y fechas.
    """
    yaml_path = str(tmp_path / "maestro.yaml")

    # Estado en disco (un proceso paralelo ya escribió)
    disk = {
        "clientes": {
            "B15234567": {
                "nombre": "Cabreiroá",
                "fecha_alta": "2026-03-01",
                "documentos_procesados": 5,
                "ultima_factura_fecha": "2026-04-28",
                "libros_activos": ["compras"],
            }
        }
    }
    guardar_maestro(yaml_path, disk)

    # Estado en memoria (este proceso): cargó cuando disk tenía 3 y subió a 3
    mem = {
        "clientes": {
            "B15234567": {
                "nombre": "Cabreiroá",
                "fecha_alta": "2026-03-01",
                "documentos_procesados": 3,
                "ultima_factura_fecha": "2026-03-15",
                "libros_activos": ["ventas"],
            }
        }
    }
    guardar_maestro(yaml_path, mem)

    final = cargar_maestro(yaml_path)
    c = final["clientes"]["B15234567"]
    # max(5, 3) = 5
    assert c["documentos_procesados"] == 5
    # max(2026-04-28, 2026-03-15) = 2026-04-28
    assert c["ultima_factura_fecha"] == "2026-04-28"
    # union {"compras"} ∪ {"ventas"}
    assert set(c["libros_activos"]) == {"compras", "ventas"}
