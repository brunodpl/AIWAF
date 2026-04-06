"""
OCR Service - CLI Entry Point (standalone)

Procesa facturas desde una carpeta usando Cloud Vision + Gemini 2.5 Flash.
Solo OCR: no ejecuta identidad ni ensamblador.
Para el pipeline completo: python -m src.pipeline

Uso:
    python -m src.phase2_ocr.main --folder horeca_sandbox/20_COMPRAS_GASTOS
"""

import argparse
import logging
import sys
from src.logging_config import setup_logging
from src.config import settings as get_settings
from .file_queue_service import run_folder

logger = logging.getLogger("pipeline.ocr")


def main():
    """Entry point standalone del OCR Service."""
    # Configurar logging antes de cualquier otra cosa
    # Excepción permitida: CLI standalone con su propio entry point
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Procesar facturas con Cloud Vision + Gemini (solo OCR)",
        epilog="Para pipeline completo (OCR + identidad + ensamblador): python -m src.pipeline"
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Carpeta con facturas a procesar"
    )
    args = parser.parse_args()

    try:
        run_folder(args.folder)
    except KeyboardInterrupt:
        logger.info("[ocr] Procesamiento interrumpido por el usuario")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"[ocr] Error fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
