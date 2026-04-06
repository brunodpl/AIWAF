"""
Entry point de la fase 3.3: Resolución semántica (concepto + cuenta contable).

Lee:     {doc_output_dir}/documento_extraido.json
         {doc_output_dir}/raw_document_ai.json (texto OCR completo)
         {doc_output_dir}/resultado_identidad_cabecera.json (NIF/nombre emisor)
Escribe: {doc_output_dir}/resultado_semantica.json

Uso standalone:
    python -m src.phase3_semantica.main --doc-id factura_001 --output-dir data/output/factura_001 --libro 20_COMPRAS_GASTOS
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.logging_config import setup_logging
from src.config import settings as get_settings

logger = logging.getLogger("pipeline.semantica")


def run_semantica(
    documento_id: str,
    doc_output_dir: str,
    libro: str,
) -> bool:
    """
    Ejecutar fase 3.3 de resolución semántica para un documento.

    Lee texto OCR y datos de identidad, resuelve concepto + cuenta_contable,
    y escribe resultado_semantica.json.

    Args:
        documento_id: ID del documento (basename del archivo original)
        doc_output_dir: Ruta al directorio de salida del documento
        libro: Libro contable destino (e.g. "20_COMPRAS_GASTOS")

    Returns:
        True si la fase completó (aunque con decisión warn/block)
        False si hubo error técnico que impide generar el artefacto
    """
    from .resolver import resolver_semantica

    doc_dir = Path(doc_output_dir)
    output_path = doc_dir / "resultado_semantica.json"

    logger.info(f"[semantica] Iniciando doc_id={documento_id} libro={libro}")

    # 1. Leer texto OCR completo desde raw_document_ai.json
    texto_ocr = _leer_texto_ocr(doc_dir)
    if not texto_ocr:
        # Fallback: intentar extraer de documento_extraido.json
        texto_ocr = _leer_texto_fallback(doc_dir)
    if not texto_ocr:
        logger.error("[semantica] No hay texto OCR disponible", exc_info=True)
        return False

    # 2. Leer NIF y nombre del emisor desde identidad
    nif_emisor, nombre_emisor = _leer_identidad_emisor(doc_dir)

    # 3. Resolver semantica
    cfg = get_settings()
    maestros_dir = Path(cfg.maestros_dir) if hasattr(cfg, 'maestros_dir') else Path("data/maestros")
    catalogo_path = str(maestros_dir / "catalogo_semantica.yaml")
    proveedores_path = str(maestros_dir / "maestro_proveedores.yaml")
    maestro_contable_path = str(maestros_dir / "maestro_contable_fiscal.yaml")
    maestro_cuentas_path = str(maestros_dir / "maestro_cuentas.yaml")

    try:
        resultado = resolver_semantica(
            texto_ocr=texto_ocr,
            nif_emisor=nif_emisor,
            nombre_emisor=nombre_emisor,
            libro=libro,
            catalogo_path=catalogo_path,
            proveedores_path=proveedores_path,
            maestro_contable_path=maestro_contable_path,
            maestro_cuentas_path=maestro_cuentas_path,
            config=cfg,
        )
    except Exception as e:
        logger.error(f"[semantica] Error en resolver_semantica: {e}", exc_info=True)
        return False

    # 4. Serializar a resultado_semantica.json
    resultado_json = {
        "documento_id": documento_id,
        "fase": "3_semantica",
        "version_politica": "v1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        "requiere_revision_humana": resultado.requiere_revision_humana,
        "motivos_revision": resultado.motivos_revision,
        "llm_usado": resultado.llm_usado,
        "tokens_llm": resultado.tokens_llm,
    }

    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(resultado_json, f, ensure_ascii=False, indent=2)
        logger.info(
            f"[semantica] Artefacto escrito: {output_path} "
            f"decision_global={resultado.decision_global}"
        )
    except Exception as e:
        logger.error(f"[semantica] Error escribiendo resultado: {e}", exc_info=True)
        return False

    return True


def _leer_texto_ocr(doc_dir: Path) -> str:
    """Leer texto OCR completo desde raw_document_ai.json."""
    bridge_path = doc_dir / "raw_document_ai.json"
    if not bridge_path.exists():
        return ""
    try:
        with open(bridge_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("text", "")
    except Exception as e:
        logger.warning(f"[semantica] Error leyendo raw_document_ai.json: {e}", exc_info=True)
        return ""


def _leer_texto_fallback(doc_dir: Path) -> str:
    """
    Fallback: construir texto descriptivo desde documento_extraido.json.
    Concatena nombre_entidad + lineas semánticas si existen.
    """
    extraido_path = doc_dir / "documento_extraido.json"
    if not extraido_path.exists():
        return ""
    try:
        with open(extraido_path, encoding="utf-8") as f:
            data = json.load(f)
        partes = []
        ident = data.get("identificacion", {})
        nombre = ident.get("nombre_entidad", {})
        if isinstance(nombre, dict):
            v = nombre.get("valor")
            if v:
                partes.append(v)
        lineas = data.get("semantica", {}).get("lineas_semanticas", [])
        for l in lineas:
            raw = l.get("concepto_raw", {})
            if isinstance(raw, dict) and raw.get("valor"):
                partes.append(raw["valor"])
        return " ".join(partes)
    except Exception:
        return ""


def _leer_identidad_emisor(doc_dir: Path) -> tuple[str | None, str | None]:
    """Leer NIF y nombre del emisor desde resultado_identidad_cabecera.json."""
    identidad_path = doc_dir / "resultado_identidad_cabecera.json"
    if not identidad_path.exists():
        return None, None
    try:
        with open(identidad_path, encoding="utf-8") as f:
            data = json.load(f)
        campos = data.get("campos", {})
        nif = campos.get("nif_entidad", {}).get("valor_final")
        nombre = campos.get("nombre_entidad", {}).get("valor_final")
        return nif, nombre
    except Exception:
        return None, None


def main() -> None:
    """Entry point CLI standalone."""
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Fase 3.3: Resolución semántica (concepto + cuenta contable)"
    )
    parser.add_argument("--doc-id", required=True, help="ID del documento (basename)")
    parser.add_argument(
        "--output-dir", required=True,
        help="Directorio con artefactos OCR/identidad"
    )
    parser.add_argument(
        "--libro", required=True,
        help="Libro contable (e.g., 20_COMPRAS_GASTOS)"
    )
    args = parser.parse_args()

    ok = run_semantica(
        documento_id=args.doc_id,
        doc_output_dir=args.output_dir,
        libro=args.libro,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
