"""Application settings, loaded once from the environment.

Everything configurable lives here. Modules import the `settings` singleton
rather than reading os.environ directly, so there is exactly one place where a
missing or malformed value can blow up — at import time, loudly, instead of
deep inside a request handler.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# .../apps/api/decisionflow/core/config.py -> repo root is 4 levels up from the
# package directory.
API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", API_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime ----------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # --- Security ---------------------------------------------------------
    secret_key: SecretStr
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    jwt_algorithm: str = "HS256"

    # --- Postgres ---------------------------------------------------------
    postgres_user: str = "decisionflow"
    postgres_password: SecretStr = SecretStr("decisionflow")
    postgres_db: str = "decisionflow"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Non-owner runtime role. RLS policies do not apply to a table's owner,
    # so the API must never connect as `postgres_user`.
    app_db_user: str = "decisionflow_app"
    app_db_password: SecretStr = SecretStr("decisionflow_app")

    # --- Redis ------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379

    # --- Object storage ---------------------------------------------------
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: SecretStr = SecretStr("minioadmin")
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "decisionflow-uploads"
    s3_region: str = "us-east-1"

    # --- Data plane -------------------------------------------------------
    duckdb_storage_path: Path = Path("./storage/duckdb")
    max_upload_bytes: int = 200 * 1024 * 1024

    # --- LLM --------------------------------------------------------------
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    gemini_reasoning_model: str = "gemini-2.5-pro"

    # --- Frontend ---------------------------------------------------------
    # Kept as a raw string: pydantic-settings tries to JSON-decode env values
    # for list-typed fields, which turns a plain comma-separated value into a
    # confusing validation error. Parsed by `cors_origin_list` instead.
    cors_origins: str = "http://localhost:3000"

    @computed_field
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @computed_field
    @property
    def duckdb_root(self) -> Path:
        """Absolute path to the per-workspace DuckDB store."""
        path = self.duckdb_storage_path
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path.resolve()

    def _dsn(self, user: str, password: SecretStr) -> str:
        return (
            f"postgresql+asyncpg://{user}:{quote_plus(password.get_secret_value())}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def database_url(self) -> str:
        """DSN used by the API at runtime — the RLS-constrained, non-owner role."""
        return self._dsn(self.app_db_user, self.app_db_password)

    @computed_field
    @property
    def migration_database_url(self) -> str:
        """DSN used by Alembic — the owner role, the only one that may run DDL.

        Deliberately the same asyncpg driver as the runtime DSN so the project
        carries one Postgres driver rather than two.
        """
        return self._dsn(self.postgres_user, self.postgres_password)

    @computed_field
    @property
    def redis_dsn(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"

    @computed_field
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def llm_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings: Settings = get_settings()
