"""
Configuración global del pipeline de facturas.

Centraliza TODAS las variables de entorno del proyecto en un único punto.
Cada fase del pipeline importa desde aquí — nunca directamente desde .env.

Carga y valida todas las variables requeridas desde .env usando pydantic_settings.
Falla rápido si falta alguna variable crítica.
"""

import os
from typing import List, Annotated
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Mapeo libro largo → corto para nombres de carpeta operario-friendly
# (trazabilidad 2.0). El audit JSONL sigue usando la forma larga para
# preservar `schema_v: 1`.
LIBRO_SHORT = {
    "20_COMPRAS_GASTOS": "compras",
    "21_VENTAS_INGRESOS": "ventas",
    "22_BIENES_INVERSION": "bienes",
    # Identidad para inputs ya en forma corta.
    "compras": "compras",
    "ventas": "ventas",
    "bienes": "bienes",
}


class Settings(BaseSettings):
    """
    Configuración global del pipeline de facturas.

    Todas las variables se cargan exclusivamente desde .env.
    Falla en construcción si falta cualquier variable requerida.
    """

    # ── Google Cloud ──────────────────────────────────────────
    google_application_credentials: str = Field(
        validation_alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    google_cloud_project_id: str = Field(
        validation_alias="GOOGLE_CLOUD_PROJECT_ID"
    )

    # ── Cloud Vision + Gemini OCR (Fase 2) ─────────────────────
    # Modelo Gemini para estructuración de texto OCR a JSON.
    # Usar versión anclada en .env (ej: gemini-2.5-flash).
    gemini_ocr_model: str = Field(
        validation_alias="GEMINI_OCR_MODEL"
    )
    gemini_ocr_location: str = Field(
        validation_alias="GEMINI_OCR_LOCATION"
    )
    vision_max_retries: int = Field(
        default=3,
        validation_alias="VISION_MAX_RETRIES"
    )

    # ── Gemini LLM árbitro (Fase 3) ──────────────────────────
    # Sin defaults — modelo y location deben ser explícitos en .env.
    # Usar siempre versión anclada (ej: gemini-2.0-flash-001), nunca alias flotante.
    gemini_arbitro_model: str = Field(
        validation_alias="GEMINI_ARBITRO_MODEL"
    )
    gemini_arbitro_location: str = Field(
        validation_alias="GEMINI_ARBITRO_LOCATION"
    )
    gemini_arbitro_max_retries: int = Field(
        validation_alias="GEMINI_ARBITRO_MAX_RETRIES"
    )

    # ── Semántica (Fase 3.3) ─────────────────────────────────
    semantica_umbral_catalogo: float = Field(
        default=0.85, validation_alias="SEMANTICA_UMBRAL_CATALOGO",
    )
    semantica_umbral_confianza_auto: float = Field(
        default=0.95, validation_alias="SEMANTICA_UMBRAL_CONFIANZA_AUTO",
    )
    semantica_umbral_confianza_warn: float = Field(
        default=0.80, validation_alias="SEMANTICA_UMBRAL_CONFIANZA_WARN",
    )

    # ── Carpetas locales (legacy — pre-trazabilidad-2.0) ──────
    # Se conservan por compatibilidad con `.env` existente. El pipeline
    # nuevo usa exclusivamente ``libros_base`` y los helpers
    # ``inbox_path``/``asientos_path``/``runtime_path``/``audit_path``.
    # Tras migración completa pueden eliminarse de `.env`.
    sandbox_base_path: str = Field(
        validation_alias="SANDBOX_BASE_PATH"
    )
    folder_compras_gastos: str = Field(
        validation_alias="FOLDER_COMPRAS_GASTOS"
    )
    folder_ventas_ingresos: str = Field(
        validation_alias="FOLDER_VENTAS_INGRESOS"
    )
    folder_bienes_inversion: str = Field(
        validation_alias="FOLDER_BIENES_INVERSION"
    )
    folder_procesadas: str = Field(
        validation_alias="FOLDER_PROCESADAS"
    )
    folder_incidencias: str = Field(
        validation_alias="FOLDER_INCIDENCIAS"
    )

    # ── Trazabilidad 2.0: estructura `libros/` ────────────────
    # Raíz única para inboxes permanentes (`libros/facturas/{libro}/`),
    # carpetas de asiento (`libros/asientos/{folder_name}/`), logs
    # (`libros/logs/audit/...`) y estado transitorio del orquestador
    # (`libros/.runtime/...`). Por defecto vive a la altura del repo.
    libros_base: str = Field(
        default="libros",
        validation_alias="LIBROS_BASE",
    )

    # ── Output Paths ──────────────────────────────────────────
    output_path: str = Field(
        validation_alias="OUTPUT_PATH"
    )
    logs_path: str = Field(
        validation_alias="LOGS_PATH"
    )

    # ── OCR Processing ────────────────────────────────────────
    extensiones_admitidas: str = Field(
        validation_alias="EXTENSIONES_ADMITIDAS"
    )
    # Umbral alineado con política operativa: autocarga solo si confianza >= umbral
    # en todos los campos críticos (ver validator.py y documentación del proyecto).
    # Tras migración a Cloud Vision + Gemini 2.5 Flash, máxima confianza asignable = 0.95.
    # Cambiar en .env si se recalibra tras benchmark.
    confianza_minima: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        validation_alias="CONFIANZA_MINIMA"
    )

    # ── Maestros ──────────────────────────────────────────────
    maestros_dir: str = Field(
        default="data/maestros",
        validation_alias="MAESTROS_DIR",
    )
    maestro_clientes_path: str = Field(
        default="data/maestros/maestro_clientes.yaml",
        validation_alias="MAESTRO_CLIENTES_PATH",
    )

    # ── Filtros de extracción (Fase 3) ────────────────────────
    numero_factura_min_length: int = Field(
        default=4,
        validation_alias="NUMERO_FACTURA_MIN_LENGTH",
        description="Longitud mínima para número de factura en regex contextual",
    )
    numero_factura_blacklist_raw: str = Field(
        default="MINC,CONTADO,DA,CISCO,IVA,TOTAL,FACTURA,FECHA,INVOICE",
        validation_alias="NUMERO_FACTURA_BLACKLIST",
        description="Palabras prohibidas en número de factura (separadas por coma)",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @field_validator("google_application_credentials")
    @classmethod
    def validate_credentials_file(cls, v: str) -> str:
        """Validar que el archivo de credenciales existe."""
        if not os.path.isfile(v):
            raise ValueError(
                f"Service account file not found: {v}\n"
                f"Please ensure GOOGLE_APPLICATION_CREDENTIALS points to a valid JSON file."
            )
        return v

    @property
    def extensiones_list(self) -> List[str]:
        """Lista de extensiones de archivo admitidas."""
        return [ext.strip().lower() for ext in self.extensiones_admitidas.split(",")]

    @property
    def numero_factura_blacklist(self) -> set[str]:
        """Set de palabras prohibidas para número de factura."""
        if not self.numero_factura_blacklist_raw:
            return set()
        return {w.strip().upper() for w in self.numero_factura_blacklist_raw.split(",")}

    def get_folder_path(self, folder_name: str) -> str:
        """
        Construir ruta absoluta para una carpeta del sandbox (legacy).

        Args:
            folder_name: Nombre de la carpeta (e.g., "20_COMPRAS_GASTOS")

        Returns:
            Ruta de la carpeta dentro de sandbox_base_path
        """
        return os.path.join(self.sandbox_base_path, folder_name)

    # ── Helpers de trazabilidad 2.0 ───────────────────────────

    def inbox_path(self, libro: str) -> str:
        """Ruta del inbox permanente para un libro.

        Acepta tanto la forma corta (`compras`/`ventas`/`bienes`) como la
        larga (`20_COMPRAS_GASTOS`/...) y devuelve siempre el path corto.
        """
        return os.path.join(self.libros_base, "facturas", LIBRO_SHORT.get(libro, libro))

    def asientos_path(self) -> str:
        """Raíz de carpetas de asiento (`libros/asientos/`)."""
        return os.path.join(self.libros_base, "asientos")

    def runtime_path(self) -> str:
        """Estado transitorio del orquestador (`libros/.runtime/`)."""
        return os.path.join(self.libros_base, ".runtime")

    def audit_path(self) -> str:
        """Raíz de logs de auditoría (`libros/logs/audit/`)."""
        return os.path.join(self.libros_base, "logs", "audit")

    def validate_paths(self) -> None:
        """
        Crear todas las rutas críticas si no existen (plug & play).

        Estructura nueva (trazabilidad 2.0):
            libros/facturas/{compras,ventas,bienes}/   ← inboxes
            libros/asientos/                           ← raíz de asientos
            libros/logs/{,audit/}                      ← logs
            libros/.runtime/                           ← estado transitorio

        Mantiene compatibilidad con la estructura legacy mientras `.env`
        siga apuntando al sandbox antiguo.
        """
        # Estructura nueva.
        for libro in ("compras", "ventas", "bienes"):
            os.makedirs(self.inbox_path(libro), exist_ok=True)
        os.makedirs(self.asientos_path(), exist_ok=True)
        os.makedirs(self.audit_path(), exist_ok=True)
        os.makedirs(self.runtime_path(), exist_ok=True)

        # Logs técnicos siguen donde estaban (logs_path puede convivir
        # apuntando a `libros/logs` o a una ruta externa).
        os.makedirs(self.logs_path, exist_ok=True)

        # Compatibilidad con el sandbox legacy mientras esté en `.env`.
        # Cuando se vacíe del entorno, este bloque queda inerte.
        if self.sandbox_base_path:
            os.makedirs(self.sandbox_base_path, exist_ok=True)

    def __repr__(self) -> str:
        """Representación segura sin exponer credenciales."""
        return (
            f"Settings(project_id={self.google_cloud_project_id}, "
            f"gemini_ocr_model={self.gemini_ocr_model}, "
            f"gemini_arbitro_model={self.gemini_arbitro_model}, "
            f"confidence_threshold={self.confianza_minima})"
        )


def get_settings() -> Settings:
    """
    Instanciar y validar Settings.

    Separar la instanciación de la definición de clase evita side-effects
    al importar el módulo en tests o en otros contextos sin .env.
    Llamar explícitamente desde main() o desde run_folder().
    """
    s = Settings()
    s.validate_paths()
    return s


# Singleton lazy: se inicializa la primera vez que se llama settings()
# desde el punto de entrada real (main.py o run_folder).
# Los tests pueden parchear settings() sin efectos colaterales.
_settings_instance: Settings | None = None


def settings() -> Settings:
    """
    Acceso global a la instancia singleton de Settings.

    Patrón función en lugar de módulo-nivel para evitar ejecución
    en tiempo de importación.
    """
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = get_settings()
    return _settings_instance


def reset_settings() -> None:
    """Resetear singleton (solo para tests). No usar en producción."""
    global _settings_instance
    _settings_instance = None
