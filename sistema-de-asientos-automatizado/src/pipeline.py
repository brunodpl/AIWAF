"""
Orquestador central del pipeline de facturas.

Conecta:
  Fase 2: OCR
  Fase 3: Resolución de campos en paralelo (identidad, fiscal, semántica)
  Fase 4: Cliente destino (secuencial, requiere identidad)
  Fase 5: Ensamblador (lee todos los artefactos)

Uso:
    python -m src.pipeline --folder horeca_sandbox/20_COMPRAS_GASTOS --libro 20_COMPRAS_GASTOS
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from src.logging_config import setup_logging
from src.audit_writer import AuditWriter
from src.phase2_ocr.file_queue_service import (
    run_ocr,
    scan_folder,
    move_file,
    ProcessingStats,
)
from src.phase2_ocr.invoice_parser_client import VisionOcrClient
from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model
from src.config import settings as get_settings
from src.phase3_identidad_cabecera.main import run_identidad
from src.phase3_fiscal.main import run_fiscal
from src.phase3_semantica.main import run_semantica
from src.phase4_customer.main import run_cliente
from src.phase4_ensamblador.ensamblador import run_ensamblador

logger = logging.getLogger("pipeline")


@dataclass
class PhaseResult:
    fase: str
    ok: bool
    motivo: str = ""


def process_document(
    file_path: str,
    folder_name: str,
    vision_client: VisionOcrClient,
    gemini_model,
    libro: str,
    output_base_path: str,
) -> tuple[str | None, list[PhaseResult]]:
    """Ejecuta OCR → Identidad → Ensamblador para un documento."""
    results: list[PhaseResult] = []

    # ── Fase 2: OCR ──────────────────────────────────────────────────────
    try:
        doc_id, doc_dir, is_valid, motivos = run_ocr(
            file_path, folder_name, vision_client, gemini_model, output_base_path
        )
        results.append(PhaseResult("ocr", ok=True))
        logger.info(f"[pipeline] OCR completado: {doc_id} (valid={is_valid})")
    except Exception as e:
        logger.error(f"[pipeline] OCR falló para {file_path}: {e}", exc_info=True)
        results.append(PhaseResult("ocr", ok=False, motivo=str(e)))
        return None, results  # Sin OCR no tiene sentido continuar

    # ── Fase 3: Resolución de campos (en paralelo) ────────────────────────
    # Los módulos de fase 3 son independientes entre sí:
    # cada uno lee documento_extraido.json (o raw_document_ai.json) y
    # escribe su propio artefacto. Se ejecutan en paralelo por diseño.
    fase3_modulos = {
        "identidad_cabecera": lambda: run_identidad(doc_id, doc_dir, usar_llm=True),
        "fiscal":             lambda: run_fiscal(doc_id, doc_dir),
        "semantica":          lambda: run_semantica(doc_id, doc_dir, libro),
    }

    fase3_resultados = {}
    with ThreadPoolExecutor(max_workers=len(fase3_modulos)) as executor:
        futuros = {
            executor.submit(fn): nombre
            for nombre, fn in fase3_modulos.items()
        }
        for futuro in as_completed(futuros):
            nombre = futuros[futuro]
            try:
                fase3_resultados[nombre] = futuro.result()
            except Exception as e:
                logger.error(
                    f"[pipeline] Fase 3 '{nombre}' lanzó excepción para {doc_id}: {e}",
                    exc_info=True,
                )
                fase3_resultados[nombre] = False

    for nombre in fase3_modulos:
        ok = fase3_resultados.get(nombre, False)
        results.append(PhaseResult(
            nombre,
            ok=ok,
            motivo="" if ok else f"error técnico en fase {nombre}",
        ))
        if not ok:
            logger.warning(f"[pipeline] {nombre} falló (error técnico) para {doc_id}")

    # ── Fase 4: Cliente destino (secuencial, requiere identidad) ────────
    ok_cliente = False
    if fase3_resultados.get("identidad_cabecera"):
        try:
            ok_cliente = run_cliente(doc_id, doc_dir, libro)
        except Exception as e:
            logger.error(
                f"[pipeline] Fase 4 cliente_destino falló para {doc_id}: {e}",
                exc_info=True,
            )
    results.append(PhaseResult(
        "cliente_destino",
        ok=ok_cliente,
        motivo="" if ok_cliente else "error o identidad no disponible",
    ))
    if not ok_cliente:
        logger.warning(f"[pipeline] cliente_destino falló para {doc_id}")

    # ── Fase 5: Ensamblado ───────────────────────────────────────────────
    ok_ensamblado = False
    try:
        ok_ensamblado = run_ensamblador(doc_id, doc_dir, libro)
    except Exception as e:
        logger.error(
            "[pipeline] Fase 5 ensamblador lanzo excepcion doc_id=%s", doc_id, exc_info=True
        )
    results.append(PhaseResult(
        "ensamblador",
        ok=ok_ensamblado,
        motivo="" if ok_ensamblado else "error tecnico en fase ensamblador",
    ))
    if not ok_ensamblado:
        logger.error(f"[pipeline] Ensamblador fallo para {doc_id}", exc_info=True)

    return doc_id, results


def run_pipeline(folder_path: str, libro: str) -> dict:
    """
    Procesa todos los archivos de una carpeta.

    Returns:
        {"total": N, "ok": N, "warn": N, "error": N}
    """
    cfg = get_settings()
    stats = ProcessingStats()
    summary = {"total": 0, "ok": 0, "warn": 0, "error": 0}

    # Inicializar escritor de auditoría para esta ejecución
    audit = AuditWriter(cfg.logs_path, libro)

    logger.info(f"[pipeline] Iniciando carpeta={folder_path} libro={libro}")

    try:
        files = scan_folder(folder_path)
    except Exception as e:
        logger.error(f"[pipeline] No se pudo escanear la carpeta: {e}", exc_info=True)
        return summary

    summary["total"] = len(files)
    if not files:
        logger.warning("[pipeline] No se encontraron archivos para procesar")
        return summary

    vision_client = VisionOcrClient()
    gemini_model = _init_gemini_model()
    folder_name = os.path.basename(os.path.normpath(folder_path))

    for file_path in files:
        doc_id, results = process_document(
            file_path, folder_name, vision_client, gemini_model, libro, cfg.output_path
        )

        # Determinar decisión final desde resultado_validacion.json
        decision = _leer_decision_global(doc_id, cfg.output_path) if doc_id else "error"

        # Registrar en auditoría de negocio (nunca interrumpe el pipeline)
        audit.write(
            doc_id=doc_id,
            file_path=file_path,
            results=results,
            decision=decision,
            output_base_path=cfg.output_path,
        )

        if decision == "auto":
            try:
                move_file(file_path, cfg.get_folder_path(cfg.folder_procesadas))
            except Exception as e:
                logger.error(
                    "[pipeline] Fallo moviendo archivo %s -> %s: %s",
                    file_path, cfg.get_folder_path(cfg.folder_procesadas), e, exc_info=True,
                )
            summary["ok"] += 1
        elif decision in ("warn", "pendiente"):
            try:
                move_file(file_path, cfg.get_folder_path(cfg.folder_incidencias))
            except Exception as e:
                logger.error(
                    "[pipeline] Fallo moviendo archivo %s -> %s: %s",
                    file_path, cfg.get_folder_path(cfg.folder_incidencias), e, exc_info=True,
                )
            summary["warn"] += 1
        else:  # block o error
            try:
                move_file(file_path, cfg.get_folder_path(cfg.folder_incidencias))
            except Exception as e:
                logger.error(
                    "[pipeline] Fallo moviendo archivo %s -> %s: %s",
                    file_path, cfg.get_folder_path(cfg.folder_incidencias), e, exc_info=True,
                )
            summary["error"] += 1

        _log_resumen_documento(doc_id, results, decision)

    _print_summary(folder_path, summary)
    return summary


def _leer_decision_global(doc_id: str, output_base: str) -> str:
    """Leer decision_global desde resultado_validacion.json. Devuelve 'error' si no existe."""
    import json
    path = Path(output_base) / doc_id / "resultado_validacion.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("decision_global", "error")
    except Exception:
        return "error"


def _log_resumen_documento(doc_id: str | None, results: list[PhaseResult], decision: str) -> None:
    fases = " | ".join(f"{r.fase}={'✓' if r.ok else '✗'}" for r in results)
    logger.info(f"[pipeline] {doc_id or 'UNKNOWN'}: [{fases}] → {decision.upper()}")


def _print_summary(folder: str, summary: dict) -> None:
    print("\n" + "=" * 60)
    print("RESUMEN PIPELINE")
    print("=" * 60)
    print(f"Carpeta:  {folder}")
    print(f"Total:    {summary['total']}")
    print(f"Auto:     {summary['ok']}")
    print(f"Revisión: {summary['warn']}")
    print(f"Error:    {summary['error']}")
    print("=" * 60)


def main() -> None:
    # Configurar logging UNA vez al inicio — reemplaza basicConfig anterior
    # Se lee cfg para obtener logs_path antes de iniciar cualquier procesamiento
    cfg = get_settings()
    setup_logging(logs_path=cfg.logs_path, level=logging.INFO)

    parser = argparse.ArgumentParser(description="Pipeline completo de facturas HORECA")
    parser.add_argument("--folder", required=True, help="Carpeta de entrada con facturas")
    parser.add_argument("--libro", required=True, help="Libro contable (e.g., 20_COMPRAS_GASTOS)")
    args = parser.parse_args()

    try:
        run_pipeline(args.folder, args.libro)
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"[pipeline] Error fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
