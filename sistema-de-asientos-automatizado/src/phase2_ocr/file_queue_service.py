"""
Servicio de cola de archivos y orquestación del pipeline OCR.

Gestiona el procesamiento secuencial de facturas:
1. Escanear carpeta de entrada
2. Para cada archivo: Vision OCR → Gemini estructuración → validate → save
3. Mover archivos (solo en ejecución standalone)
4. Generar resumen de procesamiento
"""

import logging
import os
import shutil
import json
from pathlib import Path
from typing import List, Dict, Any
from .config import settings as get_settings
from .invoice_parser_client import VisionOcrClient
from .mapper_document_ai_to_json import (
    _init_gemini_model,
    estructurar_factura,
    construir_documento_extraido,
    json_minimos,
)
from .validator import validate_documento_extraido

logger = logging.getLogger("pipeline.ocr")


class ProcessingStats:
    """Estadísticas de procesamiento de batch."""

    def __init__(self):
        self.total = 0
        self.procesadas = 0
        self.incidencias = 0
        self.errores_tecnicos = 0

    def print_summary(self, folder_name: str) -> None:
        print("\n" + "=" * 60)
        print("RESUMEN DE PROCESAMIENTO")
        print("=" * 60)
        print(f"Carpeta: {folder_name}")
        print(f"Total archivos encontrados: {self.total}")
        print(f"Procesadas correctamente:   {self.procesadas}")
        print(f"Incidencias (validación):   {self.incidencias}")
        print(f"Errores técnicos:           {self.errores_tecnicos}")
        print("=" * 60)


def scan_folder(folder_path: str) -> List[str]:
    """
    Escanear carpeta para archivos con extensión admitida.

    Returns:
        Lista ordenada alfabéticamente de rutas absolutas

    Raises:
        ValueError: Si la carpeta no existe
    """
    cfg = get_settings()

    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder does not exist: {folder_path}")

    valid_files = []

    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)

        if not os.path.isfile(file_path):
            continue

        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension in cfg.extensiones_list:
            valid_files.append(file_path)
        else:
            logger.debug(f"Skipping unsupported extension: {filename}")

    valid_files.sort()
    logger.info(f"[ocr] Found {len(valid_files)} valid files in {folder_path}")
    return valid_files


def save_json(data: Dict[str, Any], output_path: str) -> None:
    """Guardar dict como JSON formateado UTF-8."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.debug(f"[ocr] Saved JSON: {output_path}")


def move_file(source: str, destination_folder: str) -> None:
    """
    Mover archivo a carpeta de destino.

    Maneja colisiones añadiendo sufijo _1, _2, etc.
    Nunca borra archivos originales.
    """
    filename = os.path.basename(source)
    destination = os.path.join(destination_folder, filename)

    if os.path.exists(destination):
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(destination):
            new_filename = f"{base}_{counter}{ext}"
            destination = os.path.join(destination_folder, new_filename)
            counter += 1
        logger.warning(f"[ocr] Name collision, renamed to: {os.path.basename(destination)}")

    shutil.move(source, destination)
    logger.info(f"[ocr] Moved: {filename} → {os.path.basename(destination_folder)}")


def run_ocr(
    file_path: str,
    input_folder_name: str,
    vision_client: VisionOcrClient,
    gemini_model,
    output_base_path: str,
) -> tuple[str, str, bool, list[str]]:
    """
    Core del pipeline OCR sin mover archivos.

    Procesa una factura y guarda los artefactos en una subcarpeta
    dedicada por documento: {output_base_path}/{documento_id}/

    Pasos:
    1. Cloud Vision → texto plano
    2. Gemini → JSON estructurado
    3. Construir documento_extraido.json con confianzas
    4. Validar campos críticos

    Args:
        file_path: Ruta absoluta al archivo de factura
        input_folder_name: Nombre de la carpeta de entrada (para auditoría)
        vision_client: Cliente de Cloud Vision ya instanciado
        gemini_model: Modelo Gemini ya inicializado
        output_base_path: Ruta base donde crear subcarpetas por documento

    Returns:
        (documento_id, doc_output_dir, is_valid, motivos)
    """
    filename = os.path.basename(file_path)
    basename = os.path.splitext(filename)[0]
    documento_id = basename

    doc_output_dir = os.path.join(output_base_path, documento_id)
    Path(doc_output_dir).mkdir(parents=True, exist_ok=True)

    logger.info(f"[ocr] Iniciando: {filename} doc_id={documento_id}")

    # 1. Cloud Vision → texto plano
    texto_plano, num_paginas = vision_client.extraer_texto(file_path)

    # 1b. Guardar bridge JSON para Phase 3 (reemplaza raw_document_ai.json)
    # Phase 3 usa texto_completo() para regex y entities para Document AI nativo.
    # Con Cloud Vision no hay entities nativas → Phase 3 resuelve vía regex + LLM.
    bridge_doc = {
        "text": texto_plano,
        "pages": [{"pageNumber": i + 1} for i in range(num_paginas)],
        "entities": [],
    }
    bridge_path = os.path.join(doc_output_dir, "raw_document_ai.json")
    save_json(bridge_doc, bridge_path)

    # 2. Gemini → JSON estructurado
    gemini_data = estructurar_factura(texto_plano, gemini_model)

    # 3. Construir documento_extraido.json
    if gemini_data is not None:
        documento_extraido = construir_documento_extraido(
            gemini_data, filename, num_paginas, input_folder_name
        )
    else:
        documento_extraido = json_minimos(filename, input_folder_name, texto_plano)

    documento_extraido["documento_id"] = documento_id

    # 4. Validar campos críticos
    is_valid, motivos = validate_documento_extraido(documento_extraido)

    # 5. Guardar documento_extraido.json (siempre, incluso si inválido)
    extraido_output_path = os.path.join(doc_output_dir, "documento_extraido.json")
    save_json(documento_extraido, extraido_output_path)
    logger.info(f"[OCR] documento_extraido.json guardado en {doc_output_dir}")

    logger.info(
        f"[ocr] Completado: doc_id={documento_id} "
        f"valid={is_valid} motivos={len(motivos)}"
    )
    return documento_id, doc_output_dir, is_valid, motivos


def process_single_file(
    file_path: str,
    input_folder_name: str,
    stats: ProcessingStats,
    vision_client: VisionOcrClient,
    gemini_model,
) -> None:
    """
    Procesar una factura en ejecución standalone (sin pipeline completo).

    Usa run_ocr() internamente y mueve el archivo según el resultado.
    Solo OCR: no ejecuta identidad ni ensamblador.
    """
    cfg = get_settings()
    filename = os.path.basename(file_path)

    logger.info(f"[ocr] Procesando (standalone): {filename}")

    try:
        _, _, is_valid, motivos = run_ocr(
            file_path, input_folder_name, vision_client, gemini_model, cfg.output_path
        )

        if is_valid:
            move_file(file_path, cfg.get_folder_path(cfg.folder_procesadas))
            stats.procesadas += 1
            logger.info(f"[ocr] OK: {filename} → PROCESADAS")
        else:
            move_file(file_path, cfg.get_folder_path(cfg.folder_incidencias))
            stats.incidencias += 1
            logger.warning(f"[ocr] INCIDENCIA: {filename} → INCIDENCIAS. Motivos: {len(motivos)}")

    except Exception as e:
        logger.error(
            f"[ocr] ERROR TÉCNICO: {filename} — {type(e).__name__}: {e}",
            exc_info=True,
        )
        try:
            move_file(file_path, cfg.get_folder_path(cfg.folder_incidencias))
        except Exception as move_error:
            logger.critical(
                f"[ocr] CRÍTICO: no se pudo mover {filename}: {move_error}"
            )
        stats.errores_tecnicos += 1


def run_folder(folder_path: str) -> None:
    """
    Procesar todas las facturas en una carpeta (ejecución standalone OCR).

    Instancia VisionOcrClient y Gemini model una sola vez por ejecución.
    No ejecuta identidad ni ensamblador: solo OCR.
    Para pipeline completo, usar src/pipeline.py.
    """
    logger.info(f"[ocr] Iniciando procesamiento standalone: {folder_path}")

    stats = ProcessingStats()

    try:
        files = scan_folder(folder_path)
        stats.total = len(files)
    except Exception as e:
        logger.error(f"[ocr] No se pudo escanear la carpeta: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return

    if stats.total == 0:
        logger.warning("[ocr] No se encontraron archivos para procesar")
        print("Sin archivos para procesar")
        return

    vision_client = VisionOcrClient()
    gemini_model = _init_gemini_model()
    folder_name = os.path.basename(os.path.normpath(folder_path))

    for file_path in files:
        process_single_file(file_path, folder_name, stats, vision_client, gemini_model)

    stats.print_summary(folder_name)
    logger.info("[ocr] Procesamiento standalone completado")
