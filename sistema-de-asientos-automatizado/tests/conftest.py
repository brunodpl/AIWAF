"""
conftest.py raíz de la suite de tests.

Único propósito: garantizar que `src.config.Settings` puede instanciarse sin
`.env` real (caso CI/GitHub Actions y nuevos contributors). Sin esto, cualquier
módulo que importe `Settings` revienta con ValidationError al recoger el test.

Política:
- Si existe `.env` en la raíz del backend → no tocamos nada (dev local manda).
- Si no existe → sembramos defaults de TEST en `os.environ`. Los tests
  mockean todas las llamadas reales a GCP/Gemini, así que estos valores
  nunca se usan más allá de pasar la validación de pydantic-settings.

IMPORTANTE: este fichero se ejecuta UNA VEZ por sesión de pytest antes de
importar cualquier módulo de tests. Hacer el setdefault aquí, a nivel
módulo, garantiza que las env vars están presentes antes de que los tests
hagan `from src.config import Settings` o equivalentes.
"""

from __future__ import annotations

import os
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _BACKEND_ROOT / ".env"

# Defaults de TEST. Mismo nombre y forma que .env.example. Cero credenciales
# reales: el path de credenciales es ficticio porque los tests mockean
# VisionOcrClient y _init_gemini_model — nadie llega a abrir el fichero.
_TEST_DEFAULTS: dict[str, str] = {
    # Nombre `fake_service_account.json` (no `-credentials.json`) para no
    # chocar con la regla de seguridad `*-credentials.json` del .gitignore
    # del backend, pensada para evitar commits accidentales de credenciales reales.
    "GOOGLE_APPLICATION_CREDENTIALS": str(_BACKEND_ROOT / "tests" / "_fixtures" / "fake_service_account.json"),
    "GOOGLE_CLOUD_PROJECT_ID": "test-project",
    "GEMINI_OCR_MODEL": "gemini-2.5-flash",
    "GEMINI_OCR_LOCATION": "europe-west1",
    "GEMINI_ARBITRO_MODEL": "gemini-2.5-flash",
    "GEMINI_ARBITRO_LOCATION": "europe-west1",
    "GEMINI_ARBITRO_MAX_RETRIES": "2",
    "EXTENSIONES_ADMITIDAS": "pdf,jpg,jpeg,png,tiff,tif",
    "CONFIANZA_MINIMA": "0.95",
}

if not _ENV_FILE.exists():
    for key, value in _TEST_DEFAULTS.items():
        os.environ.setdefault(key, value)
