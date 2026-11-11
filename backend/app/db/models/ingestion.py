"""Phase 2 tables: provider symbol mapping, capabilities and health, ingestion jobs/batches, data-quality issues,
restatement events and the exchange-calendar overrides.

Ownership: all of these are global/system data. The one user-linked column is
`data_ingestion_jobs.requested_by_user_id` (nullable: CLI/system jobs have no user); the API only ever shows a user
the jobs they requested.
"""

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    BatchStatus,
    CalendarDayKind,
    JobStatus,
    JobType,
    Timeframe,
    VerificationStatus,
)
from app.db.models.market import enum_type
from app.marketdata.types import Capability, CapabilityStatus, IssueType, Severity

JOB_LOCK_KEY = "market-ingestion"


class InstrumentProviderSymbol(Base):
    """Explicit mapping of an internal instrument to a provider's symbol (never assumed identical)."""

    __tablename__ = "instrument_provider_symbols"
    __table_args__ = (
        UniqueConstraint("instrument_id", "provider_id", name="uq_instrument_provider_symbols_instrument_provider"),
        UniqueConstraint("provider_id", "provider_symbol", name="uq_instrument_provider_symbols_provider_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_type(VerificationStatus, "verification_status"),
        nullable=False,
        server_default=VerificationStatus.UNVERIFIED.value,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProviderCapability(Base):
    """What is known about a provider capability: documented, verified by a probe, unsupported, or unknown."""

    __tablename__ = "provider_capabilities"
    __table_args__ = (UniqueConstraint("provider_id", "capability", name="uq_provider_capabilities_provider_capability"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    capability: Mapped[Capability] = mapped_column(enum_type(Capability, "capability"), nullable=False)
    status: Mapped[CapabilityStatus] = mapped_column(enum_type(CapabilityStatus, "capability_status"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    probe_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    determined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProviderHealthState(Base):
    """One row per provider: facts about the most recent attempts. Status is *derived* from these facts."""

    __tablename__ = "provider_health_state"

    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_observed_market_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_ingestion_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DataIngestionJob(Base):
    __tablename__ = "data_ingestion_jobs"
    __table_args__ = (
        # At most one QUEUED/RUNNING job at a time, enforced by the database (protects the provider from
        # concurrent runs and the archive from interleaved writers).
        Index(
            "uq_ingestion_jobs_single_active",
            "lock_key",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[JobType] = mapped_column(enum_type(JobType, "job_type"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus, "job_status"), nullable=False, server_default=JobStatus.QUEUED.value
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    timeframe: Mapped[Timeframe] = mapped_column(enum_type(Timeframe, "timeframe"), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    lock_key: Mapped[str] = mapped_column(String(32), nullable=False, server_default=JOB_LOCK_KEY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class DataIngestionBatch(Base):
    """One provider request for one instrument, with what happened to every row it returned."""

    __tablename__ = "data_ingestion_batches"
    __table_args__ = (Index("ix_data_ingestion_batches_instrument_started", "instrument_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_ingestion_jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    timeframe: Mapped[Timeframe] = mapped_column(enum_type(Timeframe, "timeframe"), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    request_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(enum_type(BatchStatus, "batch_status"), nullable=False)
    rows_received: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    candles_inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duplicates_identical: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    restatements: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    quarantined: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    non_final_dropped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    corporate_actions_recorded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    response_first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DataQualityIssue(Base):
    """A row the pipeline rejected, quarantined or flagged, and why. Nothing is silently corrected."""

    __tablename__ = "data_quality_issues"
    __table_args__ = (Index("ix_data_quality_issues_instrument_type", "instrument_id", "issue_type"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("data_ingestion_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    issue_type: Mapped[IssueType] = mapped_column(enum_type(IssueType, "issue_type"), nullable=False)
    severity: Mapped[Severity] = mapped_column(enum_type(Severity, "severity"), nullable=False)
    bar_start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DataRestatementEvent(Base):
    """The provider reported different values for an already-archived bar. Both observations are kept."""

    __tablename__ = "data_restatement_events"
    __table_args__ = (UniqueConstraint("restated_candle_id", name="uq_data_restatement_events_restated_candle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    timeframe: Mapped[Timeframe] = mapped_column(enum_type(Timeframe, "timeframe"), nullable=False)
    bar_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    original_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="RESTRICT"), nullable=False)
    restated_candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id", ondelete="RESTRICT"), nullable=False)
    batch_id: Mapped[int] = mapped_column(ForeignKey("data_ingestion_batches.id", ondelete="RESTRICT"), nullable=False)
    changed_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)  # field -> [archived value, restated value]
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExchangeCalendarDay(Base):
    """Explicit holiday / special-session entries. EMPTY until an approved source is loaded (O13: none approved yet)."""

    __tablename__ = "exchange_calendar_days"
    __table_args__ = (
        UniqueConstraint("exchange", "day", name="uq_exchange_calendar_days_exchange_day"),
        CheckConstraint(
            "kind <> 'SPECIAL_SESSION' OR (open_time IS NOT NULL AND close_time IS NOT NULL AND close_time > open_time)",
            name="special_session_has_times",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[CalendarDayKind] = mapped_column(enum_type(CalendarDayKind, "calendar_day_kind"), nullable=False)
    open_time: Mapped[time | None] = mapped_column(Time, nullable=True)  # exchange-local
    close_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
