from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from app.db.enums import PriceBasis, Product, Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and the git-ignored .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Set only by `app.ops.staging.staging_settings` on its own in-process copy. A private attribute: no environment
    # variable, .env entry or constructor argument can set it, so the SYNTHETIC_STAGING_ONLY guard cannot be bypassed
    # by configuration.
    _staging_validator_process: bool = PrivateAttr(default=False)

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
    # Agent (Phase 5, operational): after this many consecutive failed cycles a RUNNING/PAUSED agent enters ERROR; a
    # RUNNING/PAUSED agent whose last cycle is older than the stale threshold is reported WORKER_NOT_SEEN.
    agent_max_consecutive_failures: int = Field(default=3, gt=0)
    agent_worker_stale_after_seconds: int = Field(default=900, gt=0)

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
    # B7 development decision (2026-11-11): 15 minutes (PRD section 7.4), paired with a 5-minute ingestion cadence
    # during market hours. Applies to the reference price used for decisions, sizing, marks and protective-exit
    # triggers: an older reference refuses new orders (STALE) and is never treated as current. Still to be confirmed
    # by latency measurement on the integration machine before real-environment enablement (docs/OPERATIONS.md §2).
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

    # ---- Phase 6: optional LLM narratives (explanations and journal insights) ----------------------------------
    # Disabled by default: every explanation is then the deterministic, rule-based text built from stored records. An
    # LLM only rewords those verified facts as a narrative; it never decides, sizes, submits or changes anything, and a
    # failure, timeout or invalid answer falls back to the rule-based text. No provider is mandatory:
    #   openai_compatible - any OpenAI-compatible /chat/completions server (e.g. a local Ollama or LM Studio, which need
    #                       no API key and no paid account); LLM_BASE_URL and LLM_MODEL are required;
    #   anthropic         - the Claude API through the official `anthropic` SDK (optional install; LLM_API_KEY required;
    #                       LLM_MODEL defaults to claude-opus-5-5).
    llm_provider: Literal["disabled", "openai_compatible", "anthropic"] = "disabled"
    llm_model: str | None = Field(default=None, max_length=128)
    llm_base_url: str | None = Field(default=None, max_length=500)
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=30, gt=0, le=120)
    llm_max_retries: int = Field(default=1, ge=0, le=3)  # retries of timeouts, 429 and 5xx only
    llm_max_output_tokens: int = Field(default=4000, ge=256, le=16000)
    # Per-user budget of LLM calls per rolling hour (0 = no LLM call at all); beyond it explanations are rule-based.
    llm_max_requests_per_user_per_hour: int = Field(default=30, ge=0, le=1000)
    # Journal notes are the user's private text: sent to a provider only when explicitly enabled.
    llm_include_journal_notes: bool = False

    def llm_config_problems(self) -> list[str]:
        """Why the configured LLM provider cannot be used (empty when disabled or usable). Never fatal: a misconfigured
        provider is reported and explanations stay rule-based, so the application always starts."""
        if self.llm_provider == "disabled":
            return []
        problems = []
        if self.llm_provider == "openai_compatible":
            if not self.llm_model:
                problems.append("LLM_MODEL is required for the openai_compatible provider")
            if not self.llm_base_url:
                problems.append("LLM_BASE_URL is required for the openai_compatible provider")
        if self.llm_provider == "anthropic" and not (self.llm_api_key and self.llm_api_key.get_secret_value().strip()):
            problems.append("LLM_API_KEY is required for the anthropic provider")
        if self.llm_base_url and not self.llm_base_url.startswith(("http://", "https://")):
            problems.append("LLM_BASE_URL must be an http(s) URL")
        return problems

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
            from app.exchange.costs import SYNTHETIC_STAGING_MARKER, CostScheduleError, load_cost_schedule

            try:
                schedule = load_cost_schedule(self.exchange_cost_schedule_path)
            except CostScheduleError as e:
                problems.append(str(e))
            else:
                if schedule.is_synthetic_staging_only and not self.is_staging_run:
                    problems.append(
                        f"cost schedule {schedule.version!r} is {SYNTHETIC_STAGING_MARKER}: it may only be used by the "
                        "staging validator against a '*_staging' database, never in a development or real environment"
                    )
                if on_date is not None:
                    problems.extend(schedule.problems(self.exchange_product, on_date))
        return problems

    @property
    def is_staging_run(self) -> bool:
        """True only for the staging validator's own settings (`app.ops.staging.staging_settings`): marked in-process
        by the validator, on a `*_staging` database other than the test database, with a `STAGING-ONLY:` approval
        reference. All three are required; configuration alone can satisfy at most the last two."""
        return (
            self._staging_validator_process
            and self.db_name.endswith("_staging")
            and self.db_name != self.db_test_name
            and (self.execution_approval_reference or "").startswith("STAGING-ONLY:")
        )

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
