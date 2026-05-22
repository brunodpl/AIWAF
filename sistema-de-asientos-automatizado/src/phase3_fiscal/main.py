"""
Entry point de la fase 3.2: Verificación fiscal determinista.

Lee:     {doc_output_dir}/documento_extraido.json
Escribe: {doc_output_dir}/resultado_fiscal.json

Uso standalone:
    python -m src.phase3_fiscal.main --doc-id factura_001 --output-dir data/output/factura_001
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.logging_config import setup_logging
from src.config import settings as get_settings

logger = logging.getLogger("pipeline.fiscal")


class _DecimalEncoder(json.JSONEncoder):
    """Serializa Decimal a float para JSON."""

    def default(self, o):
        if isinstance(o, Decimal):
            # Preservar como número en JSON (no string)
            return float(o)
        return super().default(o)


def run_fiscal(
    documento_id: str,
    doc_output_dir: str,
) -> bool:
    """
    Ejecutar fase 3.2 de verificación fiscal para un documento.

    Lee documento_extraido.json, verifica aritméticamente el desglose
    de IVA y escribe resultado_fiscal.json en el mismo directorio.

    Args:
        documento_id: ID del documento (basename del archivo original)
        doc_output_dir: Ruta al directorio de salida del documento

    Returns:
        True si la fase completó (aunque con decisión warn/block)
        False si hubo error técnico que impide generar el artefacto
    """
    from .verificador import verificar_fiscal, _peor_decision
    from .tipo_corrector import corregir_tipo_iva_invalido

    doc_dir = Path(doc_output_dir)
    input_path = doc_dir / "documento_extraido.json"
    output_path = doc_dir / "resultado_fiscal.json"

    logger.info(f"[fiscal] Iniciando doc_id={documento_id}")

    if not input_path.exists():
        logger.error(f"[fiscal] documento_extraido.json no encontrado: {input_path}", exc_info=True)
        return False

    try:
        with open(input_path, encoding="utf-8") as f:
            documento_extraido = json.load(f)
    except Exception as e:
        logger.error(f"[fiscal] Error leyendo documento_extraido.json: {e}", exc_info=True)
        return False

    fiscal_data = documento_extraido.get("fiscal")
    if fiscal_data is None:
        logger.error("[fiscal] Sección 'fiscal' no encontrada en documento_extraido.json", exc_info=True)
        return False

    # ── Corrección determinista de tipo IVA (F2) ANTES de validar ────────
    # El verificador no corrige; aquí derivamos el tipo de cuota/base cuando el
    # extraído no es legal. Las correcciones fuerzan revisión humana (warn).
    tipos_legales = get_settings().iva_tipos_legales
    correcciones = []
    for i, linea in enumerate(fiscal_data.get("lineas_fiscales") or []):
        r = corregir_tipo_iva_invalido(linea, tipos_legales)
        if r.corregido:
            correcciones.append((i, r))
            logger.info(f"[fiscal] Línea {i}: tipo IVA corregido — {r.motivo}")

    try:
        resultado = verificar_fiscal(fiscal_data)
    except Exception as e:
        logger.error(f"[fiscal] Error en verificar_fiscal: {e}", exc_info=True)
        return False

    if correcciones:
        lineas_campo = resultado["campos"]["lineas_fiscales"]
        if lineas_campo["decision"] == "auto":
            lineas_campo["decision"] = "warn"
        motivos_corr = "; ".join(f"línea {i}: {r.motivo}" for i, r in correcciones)
        lineas_campo["motivo"] = f"Tipo IVA derivado de cuota/base — verificar. | {lineas_campo['motivo']}"
        resultado["decision_global"] = _peor_decision(
            resultado["campos"]["total_euros"]["decision"], lineas_campo["decision"]
        )
        resultado["requiere_revision_humana"] = resultado["decision_global"] != "auto"
        resultado.setdefault("warnings", []).append(f"Tipo IVA corregido: {motivos_corr}")
        if lineas_campo["decision"] != "auto":
            resultado.setdefault("motivos_revision", []).append(f"lineas_fiscales: {motivos_corr}")

    # Añadir metadatos del documento
    resultado["documento_id"] = documento_id
    resultado["fase"] = "3_fiscal"
    resultado["version_politica"] = "v1"
    resultado["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2, cls=_DecimalEncoder)
        logger.info(
            f"[fiscal] Artefacto escrito: {output_path} "
            f"decision_global={resultado['decision_global']}"
        )
    except Exception as e:
        logger.error(f"[fiscal] Error escribiendo resultado: {e}", exc_info=True)
        return False

    return True


def main() -> None:
    """Entry point CLI standalone."""
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Fase 3.2: Verificación fiscal determinista"
    )
    parser.add_argument("--doc-id", required=True, help="ID del documento (basename)")
    parser.add_argument(
        "--output-dir", required=True,
        help="Directorio con documento_extraido.json y donde se escribirá el resultado"
    )
    args = parser.parse_args()

    ok = run_fiscal(
        documento_id=args.doc_id,
        doc_output_dir=args.output_dir,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
