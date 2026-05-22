"""
Fase 4: Resolución de cliente destino.

Entry point del módulo. Lee resultado_identidad_cabecera.json,
determina el cliente de la gestoría según el libro de origen,
y escribe resultado_cliente.json.

Uso:
    python -m src.phase4_customer.main --doc-id factura_001 --output-dir data/output/factura_001 --libro 20_COMPRAS_GASTOS
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.config import settings
from src.logging_config import setup_logging
from .resolver import resolver_cliente
from .maestro import cargar_maestro

logger = logging.getLogger("pipeline.cliente_destino")

ARTEFACTO_IDENTIDAD = "resultado_identidad_cabecera.json"
ARTEFACTO_SALIDA = "resultado_cliente.json"


def run_cliente(documento_id: str, doc_output_dir: str, libro: str) -> bool:
    """
    Ejecuta la resolución de cliente destino para un documento.

    Args:
        documento_id: ID del documento
        doc_output_dir: Directorio con artefactos de fases anteriores
        libro: Libro contable (e.g., "20_COMPRAS_GASTOS")

    Returns:
        True si completó correctamente, False si hubo error técnico.
    """
    from src.config import settings
    cfg = settings()

    doc_dir = Path(doc_output_dir)
    identidad_path = doc_dir / ARTEFACTO_IDENTIDAD
    output_path = doc_dir / ARTEFACTO_SALIDA

    logger.info(f"[cliente_destino] Iniciando para doc_id={documento_id} libro={libro}")

    # 1. Leer resultado de identidad
    try:
        with open(identidad_path, encoding="utf-8") as f:
            identidad = json.load(f)
    except FileNotFoundError:
        logger.error(f"[cliente_destino] No se encontró {ARTEFACTO_IDENTIDAD} en {doc_dir}", exc_info=True)
        return False
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[cliente_destino] Error leyendo {ARTEFACTO_IDENTIDAD}: {e}", exc_info=True)
        return False

    campos_identidad = identidad.get("campos", {})

    # 2. Cargar maestro de clientes
    maestro_path = cfg.maestro_clientes_path
    maestro = cargar_maestro(maestro_path)

    # 3. Resolver cliente
    resultado = resolver_cliente(
        campos_identidad=campos_identidad,
        libro=libro,
        maestro=maestro,
        documento_id=documento_id,
    )

    # 4. Extraer info para el log final (el maestro se actualiza solo al confirmar)
    cliente_info = resultado.get("cliente_info", {})
    decision_global = resultado.get("decision_global", "block")

    # 5. Escribir artefacto
    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        logger.info(
            f"[cliente_destino] {ARTEFACTO_SALIDA} escrito: "
            f"decision_global={decision_global} es_nuevo={cliente_info.get('es_nuevo')}"
        )
        return True
    except OSError as e:
        logger.error(f"[cliente_destino] Error escribiendo {ARTEFACTO_SALIDA}: {e}", exc_info=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Fase 4: Resolución de cliente destino")
    parser.add_argument("--doc-id", required=True, help="ID del documento")
    parser.add_argument("--output-dir", required=True, help="Directorio de artefactos del documento")
    parser.add_argument("--libro", required=True, help="Libro contable (e.g., 20_COMPRAS_GASTOS)")
    args = parser.parse_args()

    cfg = settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    ok = run_cliente(args.doc_id, args.output_dir, args.libro)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
