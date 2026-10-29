from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración leída desde variables de entorno o desde el archivo .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://escuela:escuela_dev@localhost:5432/escuela"
    # Clave para HMAC de tokens, códigos y huellas de dispositivo guardados como hash
    token_hmac_key: str = "dev-only-change-me"
    # Clave AES-256 en base64 para cifrar documentos y secretos TOTP. Generar con app.crypto.generar_clave()
    encryption_key: str = ""
    app_env: str = "development"
    timezone: str = "America/Guayaquil"

    # Sesiones
    sesion_inactividad_minutos: int = 30
    sesion_maxima_horas: int = 12
    bloqueo_intentos: int = 5
    bloqueo_minutos: int = 15

    # Enlaces de cuenta
    activacion_horas: int = 72
    recuperacion_minutos: int = 30

    # Decisión de la dirección: grado (orden) desde el que los estudiantes tienen cuenta propia
    grado_minimo_cuenta_estudiante: int = 5

    # Autorizaciones y recogida
    pickup_code_ttl_minutes: int = 10
    pickup_code_max_attempts: int = 5
    family_link_ttl_days: int = 30

    # Demo pública: muestra usuarios de prueba y el código TOTP vigente en la pantalla de acceso.
    # Nunca activar con datos reales.
    demo_mode: bool = False
    demo_password: str = ""

    @property
    def es_produccion(self) -> bool:
        return self.app_env == "production"

    def validate_for_production(self) -> None:
        """Impide arrancar en producción con claves de desarrollo o sin clave de cifrado."""
        if self.es_produccion:
            if self.token_hmac_key.startswith("dev-only"):
                raise RuntimeError("TOKEN_HMAC_KEY de desarrollo no puede usarse en producción.")
            if not self.encryption_key:
                raise RuntimeError("ENCRYPTION_KEY es obligatoria en producción.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
