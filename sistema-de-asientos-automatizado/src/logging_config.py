"""
Configuración central de logging del pipeline.

Llamar setup_logging() UNA VEZ desde el entry point real (pipeline.main).
Nunca llamar desde módulos internos ni en tiempo de importación.

Dos salidas:
  - stdout:            texto plano, para consola y desarrollo
  - logs/pipeline.jsonl: JSONL rotativo, para operación y debugging post-mortem

Retención técnica: 30 ficheros x 10 MB = ~300 MB máximo.
Esta retención es operativa, no fiscal. Los registros de auditoría fiscal
están en logs/audit/ y se gestionan por AuditWriter.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path


class _JSONLineFormatter(logging.Formatter):
    """
    Formatea cada log record como una línea JSON (JSONL).

    Campos emitidos:
      ts      — timestamp UTC ISO-8601
      level   — DEBUG / INFO / WARNING / ERROR / CRITICAL
      logger  — nombre del logger (e.g. pipeline.ocr)
      msg     — mensaje del log
      doc_id  — ID del documento si se propagó con logger.info(..., extra={"doc_id": ...})
      exc     — traceback si hay excepción, null si no
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
            "doc_id": getattr(record, "doc_id", None),
            "exc":    self.formatException(record.exc_info) if record.exc_info else None,
        }
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(logs_path: str = "logs", level: int = logging.INFO) -> None:
    """
    Configurar logging del pipeline.

    Debe llamarse UNA SOLA VEZ desde pipeline.main() antes de cualquier
    operación. Llamadas posteriores son ignoradas (idempotente).

    Args:
        logs_path: Directorio donde escribir el fichero rotativo.
                   Debe coincidir con Settings.logs_path.
        level:     Nivel mínimo de logging (default INFO).
    """
    root = logging.getLogger()

    # Idempotente: si ya tiene handlers configurados, no añadir más
    if root.handlers:
        return

    root.setLevel(level)
    Path(logs_path).mkdir(parents=True, exist_ok=True)

    # ── Handler 1: stdout — texto plano para consola ──────────────────
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(console)

    # ── Handler 2: fichero rotativo JSONL — para operación ────────────
    log_file = Path(logs_path) / "pipeline.jsonl"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB por fichero
        backupCount=30,              # ~300 MB máximo total
        encoding="utf-8",
    )
    file_handler.setFormatter(_JSONLineFormatter())
    root.addHandler(file_handler)
