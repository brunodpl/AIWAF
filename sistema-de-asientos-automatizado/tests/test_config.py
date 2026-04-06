"""
Tests para el módulo de configuración.

Valida carga de .env, validaciones y propiedades derivadas.
"""

import pytest
import os
from unittest.mock import patch, MagicMock
from pydantic import ValidationError


def _set_all_required_env(monkeypatch, tmp_path):
    """Helper: set all required env vars for a valid Settings instance."""
    sa_file = tmp_path / "service_account.json"
    sa_file.write_text('{"type": "service_account"}')

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa_file))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "test-project")
    monkeypatch.setenv("GEMINI_OCR_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_OCR_LOCATION", "europe-west1")
    monkeypatch.setenv("GEMINI_ARBITRO_MODEL", "gemini-2.0-flash-001")
    monkeypatch.setenv("GEMINI_ARBITRO_LOCATION", "europe-west1")
    monkeypatch.setenv("GEMINI_ARBITRO_MAX_RETRIES", "2")
    monkeypatch.setenv("SANDBOX_BASE_PATH", str(sandbox_dir))
    monkeypatch.setenv("FOLDER_COMPRAS_GASTOS", "20_COMPRAS_GASTOS")
    monkeypatch.setenv("FOLDER_VENTAS_INGRESOS", "21_VENTAS_INGRESOS")
    monkeypatch.setenv("FOLDER_BIENES_INVERSION", "22_BIENES_INVERSION")
    monkeypatch.setenv("FOLDER_PROCESADAS", "90_PROCESADAS")
    monkeypatch.setenv("FOLDER_INCIDENCIAS", "99_INCIDENCIAS")
    monkeypatch.setenv("OUTPUT_PATH", str(tmp_path / "output"))
    monkeypatch.setenv("LOGS_PATH", str(tmp_path / "logs"))
    monkeypatch.setenv("EXTENSIONES_ADMITIDAS", "pdf,jpg,jpeg,png,tiff,tif")
    monkeypatch.setenv("CONFIANZA_MINIMA", "0.95")


def test_config_loads_with_valid_env(monkeypatch, tmp_path):
    """Test que la configuración carga correctamente con .env válido."""
    _set_all_required_env(monkeypatch, tmp_path)

    from src import config
    import importlib
    importlib.reload(config)

    settings = config.Settings()

    assert settings.google_cloud_project_id == "test-project"
    assert settings.gemini_ocr_model == "gemini-2.5-flash"
    assert settings.gemini_ocr_location == "europe-west1"


def test_config_fails_on_missing_required_field(monkeypatch):
    """Test fail-fast si falta variable requerida."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "test-project")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_config_fails_on_nonexistent_credentials_file(monkeypatch):
    """Test validación de archivo de credenciales inexistente."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/path/file.json")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "test-project")
    monkeypatch.setenv("GEMINI_OCR_MODEL", "gemini-2.5-flash")

    from src.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert "not found" in str(exc_info.value).lower()


def test_extensiones_list_parsed_correctly(monkeypatch, tmp_path):
    """Test que extensiones_admitidas se parsea correctamente."""
    _set_all_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("EXTENSIONES_ADMITIDAS", "pdf,jpg,png")

    from src.config import Settings
    settings = Settings()

    assert settings.extensiones_list == ["pdf", "jpg", "png"]


def test_extensiones_list_handles_spaces(monkeypatch, tmp_path):
    """Test que espacios en extensiones se manejan correctamente."""
    _set_all_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("EXTENSIONES_ADMITIDAS", " pdf , jpg , png ")

    from src.config import Settings
    settings = Settings()

    assert settings.extensiones_list == ["pdf", "jpg", "png"]


def test_get_folder_path_builds_correctly(monkeypatch, tmp_path):
    """Test construcción de rutas de carpetas."""
    _set_all_required_env(monkeypatch, tmp_path)

    from src.config import Settings
    settings = Settings()

    folder_path = settings.get_folder_path(settings.folder_procesadas)
    sandbox_dir = tmp_path / "sandbox"
    expected = os.path.join(str(sandbox_dir), "90_PROCESADAS")

    assert folder_path == expected


def test_confianza_minima_validation(monkeypatch, tmp_path):
    """Test que confianza_minima está en rango válido [0, 1]."""
    _set_all_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CONFIANZA_MINIMA", "1.5")  # Invalid: > 1.0

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()
