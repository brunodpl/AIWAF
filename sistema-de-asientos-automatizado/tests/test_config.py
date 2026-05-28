"""
Tests para el módulo de configuración.

Valida carga de .env, validaciones y propiedades derivadas.
"""

import pytest
import os
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
    """Test fail-fast si falta variable requerida.

    Aislamos del `.env` real del repo con ``_env_file=None`` Y de cualquier
    env var heredada (CI, tests/conftest.py, shell, etc.). Sin estos
    delenv, las vars sembradas por tests/conftest.py para que la suite
    arranque sin .env hacen que Settings valide y el test no pueda
    verificar el fail-fast.
    """
    # Vars que tests/conftest.py siembra cuando no hay .env — limpiarlas
    # aquí para garantizar el escenario "todo ausente menos PROJECT_ID".
    for var in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_OCR_MODEL",
        "GEMINI_OCR_LOCATION",
        "GEMINI_ARBITRO_MODEL",
        "GEMINI_ARBITRO_LOCATION",
        "GEMINI_ARBITRO_MAX_RETRIES",
        "EXTENSIONES_ADMITIDAS",
        "CONFIANZA_MINIMA",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "test-project")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


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


def _set_required_env_only(monkeypatch, tmp_path):
    """Helper: solo las variables que SIGUEN siendo obligatorias tras la
    migración trazabilidad 2.0 (sin legacy SANDBOX/FOLDER_*)."""
    sa_file = tmp_path / "service_account.json"
    sa_file.write_text('{"type": "service_account"}')
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa_file))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "test-project")
    monkeypatch.setenv("GEMINI_OCR_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("GEMINI_OCR_LOCATION", "europe-west1")
    monkeypatch.setenv("GEMINI_ARBITRO_MODEL", "gemini-2.0-flash-001")
    monkeypatch.setenv("GEMINI_ARBITRO_LOCATION", "europe-west1")
    monkeypatch.setenv("GEMINI_ARBITRO_MAX_RETRIES", "2")
    monkeypatch.setenv("EXTENSIONES_ADMITIDAS", "pdf,jpg,jpeg,png")
    monkeypatch.setenv("CONFIANZA_MINIMA", "0.95")


def test_config_loads_without_legacy_env_vars(monkeypatch, tmp_path):
    """Trazabilidad 2.0: las vars SANDBOX_BASE_PATH y FOLDER_* son opcionales.
    Settings() debe instanciarse sin ellas. ``libros_base`` por defecto
    queda en ``"libros"``.

    Pasamos ``_env_file=None`` para aislarnos del `.env` real del repo
    (que sí define las legacy).
    """
    _set_required_env_only(monkeypatch, tmp_path)

    from src.config import Settings
    settings = Settings(_env_file=None)

    assert settings.sandbox_base_path is None
    assert settings.folder_compras_gastos is None
    assert settings.libros_base == "libros"


def test_get_folder_path_raises_without_sandbox_legacy(monkeypatch, tmp_path):
    """Llamar a la API legacy sin SANDBOX_BASE_PATH lanza ``RuntimeError``
    en lugar de devolver un path basura tipo ``None/foo``.
    """
    _set_required_env_only(monkeypatch, tmp_path)

    from src.config import Settings
    settings = Settings(_env_file=None)

    with pytest.raises(RuntimeError, match="SANDBOX_BASE_PATH"):
        settings.get_folder_path("90_PROCESADAS")


def test_validate_paths_creates_libros_structure(monkeypatch, tmp_path):
    """``validate_paths()`` crea la estructura libros/{facturas,asientos,
    logs/audit,.runtime} bajo ``libros_base``."""
    _set_required_env_only(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBROS_BASE", str(tmp_path / "libros"))
    monkeypatch.setenv("LOGS_PATH", str(tmp_path / "libros" / "logs"))

    from src.config import Settings
    settings = Settings(_env_file=None)
    settings.validate_paths()

    base = tmp_path / "libros"
    assert (base / "facturas" / "compras").is_dir()
    assert (base / "facturas" / "ventas").is_dir()
    assert (base / "facturas" / "bienes").is_dir()
    assert (base / "asientos").is_dir()
    assert (base / "logs" / "audit").is_dir()
    assert (base / ".runtime").is_dir()


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
