"""
Entry point de la fase 3.1: Identidad y Cabecera.

Lee:     {doc_output_dir}/raw_document_ai.json
Escribe: {doc_output_dir}/resultado_identidad_cabecera.json

Uso standalone:
    python -m src.phase3_identidad_cabecera.main --doc-id factura_001 --output-dir data/output/factura_001
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional
from src.logging_config import setup_logging
from src.config import settings as get_settings

logger = logging.getLogger("pipeline.identidad")


def run_identidad(
    documento_id: str,
    doc_output_dir: str,
    libro: Optional[str] = None,
    usar_llm: bool = True,
) -> bool:
    """
    Ejecutar fase 3.1 de identidad y cabecera para un documento.

    Lee raw_document_ai.json, resuelve campos de cabecera y escribe
    resultado_identidad_cabecera.json en el mismo directorio.

    Args:
        documento_id: ID del documento (basename del archivo original)
        doc_output_dir: Ruta al directorio de salida del documento
        libro: Libro contable (p.ej. "21_VENTAS_INGRESOS"); habilita la regla
            determinista de operadoras (operadora en rol cliente → swap).
        usar_llm: Si False, usa solo resolución determinista (útil para tests)

    Returns:
        True si la fase completó (aunque con decisión warn/block)
        False si hubo error técnico que impide generar el artefacto
    """
    from .cabecera_resolver import CabeceraResolver

    doc_dir = Path(doc_output_dir)
    raw_path = doc_dir / "raw_document_ai.json"
    output_path = doc_dir / "resultado_identidad_cabecera.json"

    logger.info(f"[identidad] Iniciando doc_id={documento_id}")

    if not raw_path.exists():
        logger.error(f"[identidad] raw_document_ai.json no encontrado: {raw_path}", exc_info=True)
        return False

    try:
        with open(raw_path, encoding="utf-8") as f:
            raw_document_ai = json.load(f)
    except Exception as e:
        logger.error(f"[identidad] Error leyendo raw_document_ai.json: {e}", exc_info=True)
        return False

    # ── Cargar documento_extraido.json de Fase 2 (opcional) ──────────────
    documento_extraido: Optional[dict] = None
    fase2_path = doc_dir / "documento_extraido.json"
    if fase2_path.exists():
        try:
            with open(fase2_path, encoding="utf-8") as f:
                documento_extraido = json.load(f)
            logger.info(f"[identidad] documento_extraido.json cargado — candidatos fase2_ocr disponibles")
        except Exception as e:
            logger.warning(
                f"[identidad] No se pudo leer documento_extraido.json: {e} — "
                f"continuando sin candidatos fase2_ocr"
            )
    else:
        logger.info(f"[identidad] documento_extraido.json no encontrado — solo fuentes 1 y 2")

    try:
        cfg = get_settings()
        operadora_nifs = cfg.operadora_nifs
        resolver = CabeceraResolver(
            usar_llm=usar_llm,
            project=cfg.google_cloud_project_id,
            credentials_path=cfg.google_application_credentials,
            gemini_model=cfg.gemini_arbitro_model,
            gemini_location=cfg.gemini_arbitro_location,
            gemini_max_retries=cfg.gemini_arbitro_max_retries,
            autofactura_marcadores=cfg.autofactura_marcadores,
            autofactura_swap_enabled=cfg.autofactura_swap_enabled,
            operadora_nifs=operadora_nifs,
        )
        resultado = resolver.resolver(
            raw_document_ai,
            documento_id=documento_id,
            documento_extraido=documento_extraido,
            libro=libro,
        )
    except Exception as e:
        logger.error(f"[identidad] Error en CabeceraResolver: {e}", exc_info=True)
        return False

    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(resultado.to_json())
        logger.info(
            f"[identidad] Artefacto escrito: {output_path} "
            f"decision_global={resultado.decision_global}"
        )
    except Exception as e:
        logger.error(f"[identidad] Error escribiendo resultado: {e}", exc_info=True)
        return False

    return True


def main() -> None:
    """Entry point CLI standalone."""
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Fase 3.1: Identidad y cabecera de facturas"
    )
    parser.add_argument("--doc-id", required=True, help="ID del documento (basename)")
    parser.add_argument(
        "--output-dir", required=True,
        help="Directorio con raw_document_ai.json y donde se escribirá el resultado"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Desactivar LLM árbitro (solo resolución determinista)"
    )
    args = parser.parse_args()

    ok = run_identidad(
        documento_id=args.doc_id,
        doc_output_dir=args.output_dir,
        usar_llm=not args.no_llm,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
