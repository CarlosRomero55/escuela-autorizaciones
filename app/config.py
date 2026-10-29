from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración leída desde variables de entorno o desde el archivo .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://escuela:escuela_dev@localhost:5432/escuela"
    secret_key: str = "dev-only-change-me"
    token_hmac_key: str = "dev-only-change-me-too"
    app_env: str = "development"
    timezone: str = "America/Guayaquil"
    pickup_code_ttl_minutes: int = 10
    pickup_code_max_attempts: int = 5
    # Clave AES-256 en base64 para cifrar documentos. Generar con app.crypto.generar_clave()
    encryption_key: str = ""
    # Días de vigencia de los enlaces de familia
    family_link_ttl_days: int = 30
    # Demo pública: muestra los usuarios de prueba en la pantalla de acceso. Nunca activar con datos reales.
    demo_mode: bool = False
    demo_password: str = ""

    def validate_for_production(self) -> None:
        """Impide arrancar en producción con claves de desarrollo o sin clave de cifrado."""
        if self.app_env == "production":
            if self.secret_key.startswith("dev-only") or self.token_hmac_key.startswith("dev-only"):
                raise RuntimeError("Las claves de desarrollo no pueden usarse en producción.")
            if not self.encryption_key:
                raise RuntimeError("ENCRYPTION_KEY es obligatoria en producción.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
