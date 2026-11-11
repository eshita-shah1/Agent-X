from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from app.db.enums import PriceBasis, Product, Timeframe

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

    # ---- Phase 4: execution price gate (open decision D4) --------------------------------
    # Fail-closed defaults: no source, no approved basis, no approved provider. With these
    # defaults the Mock Exchange cannot fill any order. Enabling fills is an explicit owner
    # decision recorded in configuration, never a code default.
    # Both enabled modes fill at the open of the first finalized bar that starts at or after the order was accepted
    # (never at the close of the bar a decision was based on). They differ only in which price basis they accept:
    #   verified_next_bar_open     - only bars whose basis is verified (with evidence) AND listed below;
    #   simulation_provider_native - only UNVERIFIED_PROVIDER_NATIVE bars (e.g. Yahoo Finance). Fills are recorded as
    #                                SIMULATION_PROVIDER_NATIVE: a retrospective simulation, never verified execution.
    # See docs/PHASE4.md and docs/D4_EXECUTION_READINESS.md.
    execution_mode: Literal["disabled", "verified_next_bar_open", "simulation_provider_native"] = "disabled"
    # Required for any enabled mode: the reference of the owner approval that enabled it (recorded with each order).
    execution_approval_reference: str | None = Field(default=None, max_length=128)
    execution_approved_price_bases: list[PriceBasis] = Field(default_factory=list)
    execution_approved_providers: list[str] = Field(default_factory=list)
    execution_candle_timeframe: Timeframe = Timeframe.M5  # canonical timeframe per the PRD
    # PROVISIONAL (PRD section 7.4: 15-minute stale threshold). Applies to the reference price used for
    # decisions, sizing and marks. Configurable, not a final rule.
    execution_max_price_age_seconds: int = Field(default=900, gt=0)

    # ---- Phase 4: transaction costs (Indian cash equities; see app/exchange/costs.py) ----------------
    # Path to the operator-maintained, effective-dated cost schedule (JSON). No rate is built into the code; while
    # the schedule is missing, invalid, or has no rate for a transaction's product/side/date, nothing executes.
    exchange_cost_schedule_path: str | None = None
    # Product executed by the Mock Exchange. Only DELIVERY is executed today (no intraday square-off or margin).
    exchange_product: Product = Product.DELIVERY
    # Optional liquidity cap: fraction of a bar's volume one fill may take. Unset means no cap (full fills).
    # Setting it enables partial fills.
    exchange_max_volume_participation: Decimal | None = Field(default=None, gt=0, le=1)

    def execution_config_problems(self, on_date: "date | None" = None) -> list[str]:
        """Safeguards for enabling execution. Empty when execution is disabled or correctly configured.

        Checked at start-up (validator below; structure only) and again whenever the execution gate is built
        (`on_date` = the trading clock's session date: the schedule must then also cover that date), because settings
        copied with `model_copy` skip validation."""
        if self.execution_mode == "disabled":
            return []
        problems = []
        if not (self.execution_approval_reference or "").strip():
            problems.append("EXECUTION_APPROVAL_REFERENCE is required to enable execution")
        if len(self.execution_approved_providers) != 1:
            problems.append("EXECUTION_APPROVED_PROVIDERS must contain exactly one provider")
        if self.execution_mode == "verified_next_bar_open" and not self.execution_approved_price_bases:
            problems.append("verified_next_bar_open requires at least one verified basis in EXECUTION_APPROVED_PRICE_BASES")
        if self.execution_mode == "simulation_provider_native" and self.execution_approved_price_bases:
            problems.append(
                "simulation_provider_native accepts only UNVERIFIED_PROVIDER_NATIVE data; "
                "EXECUTION_APPROVED_PRICE_BASES must be empty (verified bases belong to verified_next_bar_open)"
            )
        if self.exchange_product is not Product.DELIVERY:
            problems.append("only EXCHANGE_PRODUCT=DELIVERY is executable (intraday square-off is not modelled)")
        if not self.exchange_cost_schedule_path:
            problems.append("EXCHANGE_COST_SCHEDULE_PATH is required to enable execution")
        else:
            from app.exchange.costs import CostScheduleError, load_cost_schedule

            try:
                schedule = load_cost_schedule(self.exchange_cost_schedule_path)
            except CostScheduleError as e:
                problems.append(str(e))
            else:
                if on_date is not None:
                    problems.extend(schedule.problems(self.exchange_product, on_date))
        return problems

    @model_validator(mode="after")
    def _execution_safeguards(self) -> "Settings":
        problems = self.execution_config_problems()
        if problems:
            raise ValueError("execution cannot be enabled: " + "; ".join(problems))
        return self

    @field_validator("execution_candle_timeframe")
    @classmethod
    def _intraday_execution_timeframe(cls, v: Timeframe) -> Timeframe:
        # The exchange checks bar contiguity on a fixed grid; 1/5/15-minute grids align with the 09:15 IST open.
        if v not in (Timeframe.M1, Timeframe.M5, Timeframe.M15):
            raise ValueError("EXECUTION_CANDLE_TIMEFRAME must be an intraday timeframe aligned to 09:15 IST (1m, 5m, 15m)")
        return v

    @field_validator("execution_approved_price_bases")
    @classmethod
    def _unverified_basis_is_never_executable(cls, v: list[PriceBasis]) -> list[PriceBasis]:
        if PriceBasis.UNVERIFIED_PROVIDER_NATIVE in v:
            raise ValueError("UNVERIFIED_PROVIDER_NATIVE can never be approved for execution")
        return v

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
