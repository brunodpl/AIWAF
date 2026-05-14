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
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.logging_config import setup_logging
from src.audit_writer import AuditWriter
from src.phase2_ocr.file_queue_service import (
    run_ocr,
    scan_folder,
    ProcessingStats,
)
from src.phase2_ocr.invoice_parser_client import VisionOcrClient
from src.phase2_ocr.mapper_document_ai_to_json import _init_gemini_model
from src.config import LIBRO_SHORT, settings as get_settings
from src.phase3_identidad_cabecera.main import run_identidad
from src.phase3_fiscal.main import run_fiscal
from src.phase3_semantica.main import run_semantica
from src.phase4_customer.main import run_cliente
from src.phase4_ensamblador.ensamblador import run_ensamblador
from src import state_writer

logger = logging.getLogger("pipeline")

# Mapeo decision_global → status del sidecar `.state.json`.
DECISION_TO_STATUS = {
    "auto": "done",
    "warn": "review",
    "pendiente": "review",
    "block": "blocked",
    "error": "error",
}

# Slug para `numero_factura` al construir el nombre de carpeta renombrado.
import re as _re
_SLUG_RE = _re.compile(r"[^A-Za-z0-9-]+")


def _campo_valor(validacion: dict, nombre: str) -> object:
    """Lee ``campos[nombre].valor_final`` de un resultado_validacion.json."""
    campos = validacion.get("campos") or {}
    return (campos.get(nombre) or {}).get("valor_final")


def _try_rename(folder: Path, libro_short: str, validacion: dict) -> Path:
    """Renombra la carpeta de asiento al esquema descriptivo si procede.

    Reglas (trazabilidad 2.0):
        - Solo si ``decision_global ∈ {auto, warn, pendiente}``.
        - Y si están presentes ``fecha_expedicion``, ``nif_cliente``
          (NIF del cliente de la gestoría, resuelto por phase4_customer)
          y ``numero_factura`` — los tres dentro de
          ``validacion["campos"][nombre]["valor_final"]``.

    En caso contrario devuelve la carpeta sin cambios (señal visual de
    "necesita atención humana").
    """
    decision = validacion.get("decision_global")
    if decision not in {"auto", "warn", "pendiente"}:
        return folder

    fecha = _campo_valor(validacion, "fecha_expedicion")
    nif = _campo_valor(validacion, "nif_cliente")
    num = _campo_valor(validacion, "numero_factura")
    if not all([fecha, nif, num]):
        logger.info(
            "[pipeline] rename omitido (faltan datos): fecha=%s nif_cliente=%s num=%s",
            fecha, nif, num,
        )
        return folder

    num_slug = _SLUG_RE.sub("-", str(num)).strip("-")
    new_name = f"{libro_short}_{fecha}_{nif}_{num_slug}"
    return state_writer.rename(folder, new_name)


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
    status_file: str | None = None,
) -> tuple[str | None, str | None, list[PhaseResult]]:
    """Ejecuta OCR → Identidad → Ensamblador para un documento.

    Devuelve ``(doc_id, doc_dir, results)``. ``doc_dir`` puede usarse
    para renombrar la carpeta de asiento o leer artefactos posteriores.
    """
    results: list[PhaseResult] = []
    libro_short = LIBRO_SHORT.get(libro, libro)

    # ── Fase 2: OCR ──────────────────────────────────────────────────────
    try:
        doc_id, doc_dir, is_valid, motivos = run_ocr(
            file_path,
            folder_name,
            vision_client,
            gemini_model,
            output_base_path,
            libro=libro_short,
        )
        results.append(PhaseResult("ocr", ok=True))
        logger.info(f"[pipeline] OCR completado: {doc_id} (valid={is_valid})")
    except Exception as e:
        logger.error(f"[pipeline] OCR falló para {file_path}: {e}", exc_info=True)
        results.append(PhaseResult("ocr", ok=False, motivo=str(e)))
        # Fallback: registrar el error en un sidecar usando el basename
        # como doc_id si la carpeta se llegó a crear, para mantener trazabilidad.
        basename = os.path.splitext(os.path.basename(file_path))[0]
        fallback_dir = Path(output_base_path) / f"{libro_short}_{basename}"
        try:
            if not fallback_dir.exists():
                fallback_dir.mkdir(parents=True, exist_ok=True)
                state_writer.init(
                    fallback_dir,
                    doc_id=basename,
                    file_origin=os.path.relpath(file_path).replace(os.sep, "/"),
                )
            state_writer.append(
                fallback_dir,
                {"status": "error", "motivo": f"{type(e).__name__}: {e}"},
            )
        except Exception:
            logger.critical(
                "[pipeline] no se pudo registrar evento error en sidecar para %s",
                file_path,
                exc_info=True,
            )
        return None, str(fallback_dir), results  # Sin OCR no tiene sentido continuar

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
    FASE3_TIMEOUT = 300  # 5 minutos por módulo
    with ThreadPoolExecutor(max_workers=len(fase3_modulos)) as executor:
        futuros = {
            executor.submit(fn): nombre
            for nombre, fn in fase3_modulos.items()
        }
        for futuro in as_completed(futuros):
            nombre = futuros[futuro]
            try:
                fase3_resultados[nombre] = futuro.result(timeout=FASE3_TIMEOUT)
            except TimeoutError:
                logger.error(
                    f"[pipeline] Fase 3 '{nombre}' excedió timeout ({FASE3_TIMEOUT}s) para {doc_id}"
                )
                fase3_resultados[nombre] = False
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

    if status_file:
        _update_status_file(
            status_file,
            doc_id=doc_id,
            phases=[{"fase": r.fase, "ok": r.ok, "motivo": r.motivo} for r in results],
        )

    return doc_id, doc_dir, results


def run_pipeline(folder_path: str, libro: str, status_file: str | None = None) -> dict:
    """
    Procesa todos los archivos de una carpeta.

    Returns:
        {"total": N, "ok": N, "warn": N, "error": N}
    """
    cfg = get_settings()
    stats = ProcessingStats()
    summary = {"total": 0, "ok": 0, "warn": 0, "error": 0}

    # Inicializar escritor de auditoría para esta ejecución.
    # Trazabilidad 2.0: el audit JSONL vive bajo `libros/logs/audit/`,
    # no bajo el LOGS_PATH legacy.
    audit = AuditWriter(cfg.audit_path(), libro)

    logger.info(f"[pipeline] Iniciando carpeta={folder_path} libro={libro}")

    try:
        files = scan_folder(folder_path)
    except Exception as e:
        logger.error(f"[pipeline] No se pudo escanear la carpeta: {e}", exc_info=True)
        if status_file:
            _update_status_file(
                status_file,
                status="error",
                error_message=str(e),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        return summary

    # Idempotencia: si una factura ya tiene un asiento confirmado por el
    # operario (`.state.json` con último status="done"), saltarla. Evita que
    # un nuevo escaneo reprocese facturas anteriores que la UI ya oculta del
    # inbox.
    asientos_root_for_skip = cfg.asientos_path()
    pending_files: list[str] = []
    skipped = 0
    for fp in files:
        doc_id = os.path.basename(fp).rsplit(".", 1)[0]
        existing_folder = state_writer.find_folder_by_doc_id(asientos_root_for_skip, doc_id)
        if existing_folder and state_writer.is_done(existing_folder):
            skipped += 1
            logger.info(f"[pipeline] Saltando '{doc_id}' — asiento ya confirmado (done)")
            continue
        pending_files.append(fp)

    if skipped:
        logger.info(f"[pipeline] {skipped} factura(s) ya procesadas — omitidas")
    files = pending_files

    summary["total"] = len(files)
    if not files:
        logger.warning("[pipeline] No se encontraron archivos para procesar")
        if status_file:
            _update_status_file(
                status_file,
                status="completed",
                total=0,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        return summary

    if status_file:
        _update_status_file(
            status_file,
            status="running",
            total=len(files),
            processed=0,
            current_file=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=None,
            error_message=None,
        )

    # Guardia de reinicio: ".pending_confirm.json" señala que hay un lote en
    # curso a la espera de confirmación humana. POST /api/pipeline/confirm
    # lo elimina al cerrar. Si el contenedor se reinicia, el endpoint
    # /api/pipeline/status lo reporta para que la UI avise al operario.
    pending_doc_ids = [os.path.basename(f).rsplit(".", 1)[0] for f in files]
    _write_pending_confirm(cfg, pending_doc_ids)

    vision_client = VisionOcrClient()
    gemini_model = _init_gemini_model()
    folder_name = os.path.basename(os.path.normpath(folder_path))
    libro_short = LIBRO_SHORT.get(libro, libro)
    asientos_root = cfg.asientos_path()

    try:
        for file_path in files:
            if status_file:
                _update_status_file(status_file, current_file=os.path.basename(file_path))

            doc_id, doc_dir, results = process_document(
                file_path, folder_name, vision_client, gemini_model, libro, asientos_root,
                status_file=status_file,
            )

            # Determinar decisión final desde resultado_validacion.json
            decision = _leer_decision_global(doc_dir) if doc_id else "error"

            # ── Registrar estado en el sidecar y renombrar si procede ──
            #
            # Trazabilidad 2.0: el PDF NO se mueve. El estado del documento
            # vive en `.state.json` dentro de su carpeta de asiento, y la
            # carpeta se renombra al esquema operario-friendly si hay datos
            # suficientes.
            folder_final = Path(doc_dir) if doc_dir else None
            if folder_final and folder_final.exists():
                validacion = _leer_validacion(folder_final)
                status = DECISION_TO_STATUS.get(decision, "error")
                event: dict = {"status": status, "decision": decision}
                motivos = validacion.get("motivos_revision") if validacion else None
                if motivos:
                    event["motivos"] = motivos
                try:
                    state_writer.append(folder_final, event)
                except Exception:
                    logger.error(
                        "[pipeline] no se pudo registrar status=%s en sidecar para %s",
                        status, doc_id, exc_info=True,
                    )

                # Rename si la decisión y los campos lo permiten.
                if validacion:
                    try:
                        folder_final = _try_rename(folder_final, libro_short, validacion)
                    except Exception:
                        logger.warning(
                            "[pipeline] rename falló para %s; carpeta queda como %s",
                            doc_id, folder_final.name, exc_info=True,
                        )

            # Registrar en auditoría de negocio (nunca interrumpe el pipeline)
            audit.write(
                doc_id=doc_id,
                file_path=file_path,
                results=results,
                decision=decision,
                output_base_path=asientos_root,
                folder_name=folder_final.name if folder_final else None,
            )

            # Contadores de resumen
            if decision == "auto":
                summary["ok"] += 1
            elif decision in ("warn", "pendiente"):
                summary["warn"] += 1
            else:
                summary["error"] += 1

            _log_resumen_documento(doc_id, results, decision)

            if status_file:
                _update_status_file(status_file, processed=summary["ok"] + summary["warn"] + summary["error"])

        _print_summary(folder_path, summary)

        if status_file:
            _update_status_file(
                status_file,
                status="completed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                summary=summary,
            )
    except Exception as e:
        logger.error(f"[pipeline] Error en run_pipeline: {e}", exc_info=True)
        if status_file:
            _update_status_file(
                status_file,
                status="error",
                error_message=str(e),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        raise

    return summary


def _leer_validacion(doc_dir: str | Path) -> dict | None:
    """Lee ``resultado_validacion.json`` de la carpeta de asiento. ``None`` si no existe."""
    path = Path(doc_dir) / "resultado_validacion.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _leer_decision_global(doc_dir: str | Path) -> str:
    """Devuelve ``decision_global`` desde la validación o ``'error'`` si no existe."""
    data = _leer_validacion(doc_dir)
    if not data:
        return "error"
    return data.get("decision_global", "error")


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


def _write_pending_confirm(cfg, doc_ids: list[str]) -> None:
    """Marca el lote como pendiente de confirmación humana."""
    try:
        runtime_dir = Path(cfg.runtime_path())
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / ".pending_confirm.json"
        payload = {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "doc_ids_lote": doc_ids,
        }
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[pipeline] no se pudo escribir .pending_confirm.json: {e}")


def _update_status_file(status_file: str, **kwargs) -> None:
    """Write/update a JSON status file atomically. Never raises; logs errors."""
    try:
        data = {}
        if os.path.exists(status_file):
            with open(status_file, encoding="utf-8") as f:
                data = json.load(f)
        data.update(kwargs)
        tmp_path = status_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, status_file)
    except Exception:
        logger.debug(f"[pipeline] Failed to update status file {status_file}", exc_info=True)


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
