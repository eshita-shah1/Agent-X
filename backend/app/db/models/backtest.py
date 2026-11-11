"""Backtest runs and their results (Phase 3, Step 4B; migration 0009).

Every table is user-owned. Result rows carry their run's owner; the insert trigger accepts a result row only for a
RUNNING run with the same owner, so a result can never be attached to another user's run. Rows are immutable once written (database triggers): a run's
request and fingerprint never change, a finished run never changes, and result rows can only be inserted while
their run is RUNNING and are never updated or deleted. Money and prices use unconstrained NUMERIC so the engine's
Decimal values are stored exactly (no rounding).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.backtest.costs import CostBasis
from app.backtest.model import BenchmarkStatus, CapitalSource, EventType, ExitReason, ExitTiming
from app.db.base import Base, UserOwnedMixin
from app.db.enums import BacktestRunStatus

EXACT = Numeric()  # arbitrary precision: stores Decimal values exactly


def label_type(cls: type[enum.Enum], name: str) -> Enum:
    """VARCHAR + CHECK like `enum_type`, with room for the engine's longer labels."""
    return Enum(cls, name=name, native_enum=False, create_constraint=True, length=48, values_callable=lambda e: [m.value for m in e])


def _run_id(primary_key: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False, primary_key=primary_key, index=not primary_key)


class BacktestRun(UserOwnedMixin, Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        # At most one QUEUED/RUNNING backtest per user (approved: user-initiated background jobs, one active per user).
        Index("uq_backtest_runs_single_active", "user_id", unique=True, postgresql_where=text("status IN ('QUEUED','RUNNING')")),
        Index("ix_backtest_runs_user_created", "user_id", "created_at"),
        CheckConstraint("capital > 0", name="capital_positive"),
        CheckConstraint("requested_end >= requested_start", name="period_ordered"),
        CheckConstraint("char_length(config_fingerprint) = 64", name="config_fingerprint_sha256"),
        CheckConstraint(
            "(status = 'SUCCEEDED') = (data_fingerprint IS NOT NULL AND result_fingerprint IS NOT NULL)",
            name="fingerprints_iff_succeeded",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[BacktestRunStatus] = mapped_column(
        label_type(BacktestRunStatus, "backtest_run_status"), nullable=False, server_default=BacktestRunStatus.QUEUED.value
    )
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)  # canonical, complete request (config document)
    instrument_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    requested_start: Mapped[date] = mapped_column(Date, nullable=False)
    requested_end: Mapped[date] = mapped_column(Date, nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    capital: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    capital_source: Mapped[CapitalSource] = mapped_column(label_type(CapitalSource, "capital_source"), nullable=False)
    cost_basis: Mapped[CostBasis] = mapped_column(label_type(CostBasis, "cost_basis"), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(48), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(16), nullable=False)
    fill_model: Mapped[str] = mapped_column(String(48), nullable=False)
    sizing_model: Mapped[str] = mapped_column(String(48), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disclosures: Mapped[list] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestInstrumentResult(UserOwnedMixin, Base):
    __tablename__ = "backtest_instrument_results"
    __table_args__ = (UniqueConstraint("run_id", "instrument_id", name="uq_backtest_instrument_results_run_instrument"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = _run_id()
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    data_quality: Mapped[dict] = mapped_column(JSONB, nullable=False)
    benchmark_status: Mapped[BenchmarkStatus] = mapped_column(label_type(BenchmarkStatus, "benchmark_status"), nullable=False)
    benchmark: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    final_equity: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    benchmark_final_value: Mapped[Decimal] = mapped_column(EXACT, nullable=False)


class BacktestSelectionDecision(UserOwnedMixin, Base):
    __tablename__ = "backtest_selection_decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "instrument_id", "session_date", name="uq_backtest_selection_decisions_session"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = _run_id()
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_config_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[dict] = mapped_column(JSONB, nullable=False)  # SelectionDecision.as_dict(), unchanged


class BacktestTrade(UserOwnedMixin, Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (
        UniqueConstraint("run_id", "instrument_id", "seq", name="uq_backtest_trades_seq"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = _run_id()
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    config_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_signal_bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_signal_reason: Mapped[str] = mapped_column(String(48), nullable=False)
    entry_reference_price: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    entry_bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stop_level: Mapped[Decimal | None] = mapped_column(EXACT, nullable=True)
    target_level: Mapped[Decimal | None] = mapped_column(EXACT, nullable=True)
    entry_fees: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    exit_signal_bar_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_signal_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    exit_reference_price: Mapped[Decimal | None] = mapped_column(EXACT, nullable=True)
    exit_bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_timing: Mapped[ExitTiming] = mapped_column(label_type(ExitTiming, "exit_timing"), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    exit_reason: Mapped[ExitReason] = mapped_column(label_type(ExitReason, "exit_reason"), nullable=False)
    exit_fees: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(EXACT, nullable=False)


class BacktestEvent(UserOwnedMixin, Base):
    __tablename__ = "backtest_events"
    __table_args__ = (UniqueConstraint("run_id", "instrument_id", "seq", name="uq_backtest_events_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = _run_id()
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[EventType] = mapped_column(label_type(EventType, "backtest_event_type"), nullable=False)
    config_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)


class BacktestEquityPoint(UserOwnedMixin, Base):
    __tablename__ = "backtest_equity_points"

    run_id: Mapped[uuid.UUID] = _run_id(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), primary_key=True)
    bar_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    cash: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    close: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    equity: Mapped[Decimal] = mapped_column(EXACT, nullable=False)
    benchmark_equity: Mapped[Decimal] = mapped_column(EXACT, nullable=False)


RESULT_TABLES = (
    "backtest_instrument_results", "backtest_selection_decisions", "backtest_trades", "backtest_events", "backtest_equity_points",
)
