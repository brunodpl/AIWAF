"""
Wrapper de compatibilidad — re-exporta desde src.config.

Permite que los módulos internos de phase2_ocr sigan usando:
    from .config import settings
    from .config import get_settings, Settings

Toda la configuración real vive en src/config.py.
"""

from src.config import Settings, get_settings, settings, reset_settings

__all__ = ["Settings", "get_settings", "settings", "reset_settings"]
