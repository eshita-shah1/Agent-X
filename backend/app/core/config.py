from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and the git-ignored .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = "development"

    db_host: str
    db_port: int
    db_user: str
    db_password: SecretStr
    db_name: str
    db_test_name: str
    # Operational setting (not a trading parameter): without a timeout an unreachable database
    # makes connections, and therefore /health/ready, hang for minutes.
    db_connect_timeout_seconds: int = Field(default=5, gt=0)

    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "agent-x"
    access_token_expire_minutes: int = Field(gt=0)

    # --- Market data (Phase 2) -------------------------------------------------------------
    # Values marked PROVISIONAL were retained by the owner for Phase 2. They influence when a bar counts as final
    # and when data counts as stale. They only *describe* data: they are not guaranteed provider behaviour and
    # not trading-risk rules, and need a separate review before any trading engine relies on them.
    marketdata_provider: str = "yahoo"
    # Measured 2026-11-11: bars settled within ~31 s after their end; 60 s is the provisional margin.
    marketdata_finalization_margin_seconds: int = Field(default=60, ge=0)  # PROVISIONAL
    marketdata_stale_after_minutes: int = Field(default=15, gt=0)  # PROVISIONAL
    marketdata_provider_unavailable_after_failures: int = Field(default=3, gt=0)  # PROVISIONAL
    # operational (not trading-relevant)
    marketdata_incremental_overlap_hours: int = Field(default=24, ge=0)
    marketdata_job_heartbeat_timeout_seconds: int = Field(default=300, gt=0)
    marketdata_request_pause_seconds: float = Field(default=1.5, ge=0)
    # Backtests (Phase 3, operational): a QUEUED/RUNNING run whose heartbeat is older than this is presumed dead
    # and is marked INTERRUPTED when its owner next starts a run.
    backtest_job_heartbeat_timeout_seconds: int = Field(default=600, gt=0)

    @field_validator("jwt_secret_key")
    @classmethod
    def _secret_is_long_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("jwt_algorithm")
    @classmethod
    def _symmetric_algorithms_only(cls, v: str) -> str:
        if v not in {"HS256", "HS384", "HS512"}:
            raise ValueError("JWT_ALGORITHM must be one of HS256, HS384, HS512")
        return v

    def _url(self, database: str) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=database,
        )

    @property
    def database_url(self) -> URL:
        return self._url(self.db_name)

    @property
    def test_database_url(self) -> URL:
        return self._url(self.db_test_name)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
