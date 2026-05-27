"""
Cliente para Google Cloud Vision API — extracción de texto de facturas.

Reemplaza Document AI Invoice Parser por Cloud Vision document_text_detection.
Cloud Vision extrae texto plano; la estructuración a JSON se delega a Gemini.

NO instanciar a nivel de módulo: instanciar desde file_queue_service
o main para evitar side-effects en imports (tests, CI).
"""

import logging
import time
import fitz  # PyMuPDF
from google.cloud import vision
from google.oauth2 import service_account
from typing import List, Tuple
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


def _pdf_page_chunks(content: bytes, limit: int) -> List[bytes]:
    """Trocear un PDF en lotes de ≤``limit`` páginas.

    Devuelve una lista de PDFs (bytes), cada uno con como máximo ``limit``
    páginas, en orden. Si el PDF cabe en un solo lote, devuelve un único
    elemento. La ruta síncrona de Cloud Vision rechaza PDFs con más páginas
    que el límite de la API, así que el OCR los procesa por lotes.
    """
    chunks: List[bytes] = []
    with fitz.open(stream=content, filetype="pdf") as src:
        total = src.page_count
        for start in range(0, total, limit):
            end = min(start + limit - 1, total - 1)
            out = fitz.open()
            try:
                out.insert_pdf(src, from_page=start, to_page=end)
                chunks.append(out.tobytes())
            finally:
                out.close()
    return chunks


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
        self.vision_sync_page_limit = cfg.vision_sync_page_limit
        self.vision_timeout_seconds = cfg.vision_timeout_seconds
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
        response = self.client.document_text_detection(
            image=image, timeout=self.vision_timeout_seconds
        )

        if response.error.message:
            raise Exception(f"Vision API error: {response.error.message}")

        text = ""
        if response.full_text_annotation:
            text = response.full_text_annotation.text
        return text

    def _extract_text_pdf(self, content: bytes) -> Tuple[str, int]:
        """
        Extraer texto de PDF (potencialmente multipágina) vía batch annotate.

        La ruta síncrona de Cloud Vision rechaza PDFs con más páginas que
        ``vision_sync_page_limit``. Los PDFs que superen el límite se trocean
        en lotes de ≤límite y el texto se concatena con numeración de página
        global continua (factura Gadis multipágina y similares).

        Returns:
            Tupla de (texto_concatenado, num_paginas)
        """
        # __init__ siempre setea vision_sync_page_limit; acceso directo para
        # que falle ruidoso si alguna vez dejara de hacerlo (no enmascarar).
        page_limit = self.vision_sync_page_limit

        with fitz.open(stream=content, filetype="pdf") as doc:
            total_pages = doc.page_count

        if total_pages <= page_limit:
            chunks = [content]
        else:
            chunks = _pdf_page_chunks(content, page_limit)
            logger.info(
                "[Vision] PDF de %d páginas troceado en %d lote(s) de ≤%d para "
                "Cloud Vision síncrono",
                total_pages, len(chunks), page_limit,
            )

        pages_text = []
        num_paginas = 0

        for chunk in chunks:
            input_config = vision.InputConfig(
                content=chunk,
                mime_type="application/pdf"
            )
            feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
            request = vision.AnnotateFileRequest(
                input_config=input_config,
                features=[feature],
            )

            response = self.client.batch_annotate_files(
                requests=[request], timeout=self.vision_timeout_seconds
            )

            for file_response in response.responses:
                # Error a nivel de fichero (p.ej. "exceeds page limit"): es
                # determinista, no transitorio — abortar con motivo en vez de
                # devolver texto parcial silenciosamente.
                if file_response.error.message:
                    raise Exception(
                        f"Vision API error (fichero): {file_response.error.message}"
                    )
                for page_response in file_response.responses:
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
