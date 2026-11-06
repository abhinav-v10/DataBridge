from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (prefix DATABRIDGE_) or a .env file."""

    model_config = SettingsConfigDict(env_prefix="DATABRIDGE_", env_file=".env", extra="ignore")

    # Where DataBridge keeps its own state (connections, runs, exceptions...).
    metadata_db_url: str = "sqlite:///./databridge_meta.db"
    # Fernet key used to encrypt stored connection passwords. REQUIRED outside tests.
    secret_key: str = ""
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
