"""
Cliente para Google Cloud Vision API — extracción de texto de facturas.

Reemplaza Document AI Invoice Parser por Cloud Vision document_text_detection.
Cloud Vision extrae texto plano; la estructuración a JSON se delega a Gemini.

NO instanciar a nivel de módulo: instanciar desde file_queue_service
o main para evitar side-effects en imports (tests, CI).
"""

import logging
import time
from google.cloud import vision
from google.oauth2 import service_account
from typing import Tuple
from .config import settings as get_settings

logger = logging.getLogger("pipeline.ocr")


# MIME type mapping por extensión de archivo
MIME_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}


class VisionOcrClient:
    """
    Wrapper para Google Cloud Vision API — extracción de texto.

    Gestiona autenticación con service account y llamadas al API.
    Instanciar una vez desde el punto de entrada del pipeline.
    """

    def __init__(self):
        """Inicializar cliente con credenciales explícitas."""
        cfg = get_settings()

        self.credentials = service_account.Credentials.from_service_account_file(
            cfg.google_application_credentials
        )
        self.client = vision.ImageAnnotatorClient(
            credentials=self.credentials
        )
        self.max_retries = cfg.vision_max_retries
        logger.info("[Vision] Cloud Vision client initialized")

    def get_mime_type(self, file_path: str) -> str:
        """
        Determinar MIME type desde extensión de archivo.

        Raises:
            ValueError: Si la extensión no está soportada
        """
        extension = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        mime_type = MIME_TYPES.get(extension)

        if not mime_type:
            raise ValueError(
                f"Unsupported file extension: '{extension}'. "
                f"Supported: {', '.join(MIME_TYPES.keys())}"
            )
        return mime_type

    def _read_file(self, file_path: str) -> bytes:
        """Leer archivo y devolver contenido binario."""
        try:
            with open(file_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            logger.error(f"[Vision] Archivo no encontrado: {file_path}", exc_info=True)
            raise ValueError(f"File not found: {file_path}")
        except Exception as e:
            logger.error(f"[Vision] Error al leer archivo {file_path}: {e}", exc_info=True)
            raise ValueError(f"Cannot read file: {e}")

    def _retry_call(self, func, file_path: str):
        """Ejecutar función con reintentos y backoff exponencial."""
        for intento in range(self.max_retries):
            try:
                return func()
            except Exception as e:
                if intento == self.max_retries - 1:
                    logger.error(f"[Vision] Fallo persistente en {file_path}: {e}", exc_info=True)
                    raise
                wait = 2 ** intento
                logger.warning(
                    f"[Vision] Reintento {intento + 1}/{self.max_retries} "
                    f"en {wait}s: {e}"
                )
                time.sleep(wait)

    def _extract_text_image(self, content: bytes) -> str:
        """Extraer texto de imagen o PDF de una sola página (síncrono)."""
        image = vision.Image(content=content)
        response = self.client.document_text_detection(image=image)

        if response.error.message:
            raise Exception(f"Vision API error: {response.error.message}")

        text = ""
        if response.full_text_annotation:
            text = response.full_text_annotation.text
        return text

    def _extract_text_pdf(self, content: bytes) -> Tuple[str, int]:
        """
        Extraer texto de PDF (potencialmente multipágina) vía batch annotate.

        Returns:
            Tupla de (texto_concatenado, num_paginas)
        """
        input_config = vision.InputConfig(
            content=content,
            mime_type="application/pdf"
        )
        feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
        request = vision.AnnotateFileRequest(
            input_config=input_config,
            features=[feature],
        )

        response = self.client.batch_annotate_files(requests=[request])

        pages_text = []
        num_paginas = 0

        for file_response in response.responses:
            for i, page_response in enumerate(file_response.responses):
                num_paginas += 1
                if page_response.error.message:
                    logger.warning(
                        f"[Vision] Error en página {num_paginas}: "
                        f"{page_response.error.message}"
                    )
                    continue
                if page_response.full_text_annotation:
                    page_text = page_response.full_text_annotation.text
                    pages_text.append(f"--- PÁGINA {num_paginas} ---\n{page_text}")

        texto = "\n".join(pages_text)
        return texto, max(num_paginas, 1)

    def extraer_texto(self, file_path: str) -> Tuple[str, int]:
        """
        Extraer texto de una factura (PDF o imagen).

        Args:
            file_path: Ruta local del archivo a procesar

        Returns:
            Tupla de (texto_plano, num_paginas)

        Raises:
            ValueError: Si el archivo no existe o extensión no soportada
            Exception: Si la llamada al API falla tras reintentos
        """
        mime_type = self.get_mime_type(file_path)
        content = self._read_file(file_path)
        filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path.rsplit("\\", 1)[-1]

        is_pdf = mime_type == "application/pdf"
        modo = "batch PDF" if is_pdf else "imagen síncrono"
        logger.info(f"[Vision] Procesando {filename} — modo {modo}")

        if is_pdf:
            def call():
                return self._extract_text_pdf(content)

            texto, num_paginas = self._retry_call(call, file_path)
        else:
            def call():
                return self._extract_text_image(content), 1

            texto, num_paginas = self._retry_call(call, file_path)

        logger.info(f"[Vision] Texto extraído: {len(texto)} caracteres, {num_paginas} página(s)")
        return texto, num_paginas
