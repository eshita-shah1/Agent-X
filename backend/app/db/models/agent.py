"""Phase 5 tables: the autonomous agent's lifecycle, configuration versions, cycles, decisions and events.

All are user-owned. They never duplicate Phase 4 records: orders, fills, positions, the cash ledger and risk decisions
stay authoritative and are only referenced (foreign keys). Candles are referenced by id and observation version, never
copied. Guards (migration 0012):
  * one agent per user (unique user_id); status transitions are checked by a trigger; agents are never deleted;
  * configuration versions, decisions and events are append-only; a finished cycle is frozen;
  * at most one RUNNING cycle per agent, one evaluation per agent, instrument and bar, one selection per agent,
    instrument and session, and one decision per client order id (no candle is processed twice, no order is
    submitted twice).
"""

import uuid
from datetime import date, datetime

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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UserOwnedMixin
from app.db.enums import (
    AgentCycleStatus,
    AgentDecisionKind,
    AgentDecisionOutcome,
    AgentInstrumentStatus,
    AgentMode,
    AgentStatus,
)
from app.db.models.market import enum_type as _enum


def _ts(nullable: bool = False, default_now: bool = False) -> Mapped:
    if default_now:
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=nullable)
    return mapped_column(DateTime(timezone=True), nullable=nullable)


def _agent_id(index: bool = True) -> Mapped[int]:
    return mapped_column(ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False, index=index)


class AgentInstance(UserOwnedMixin, Base):
    __tablename__ = "agent_instances"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_agent_instances_user_id"),  # one agent per account (initially)
        CheckConstraint("status NOT IN ('RUNNING', 'PAUSED') OR mode IS NOT NULL", name="active_has_mode"),
        CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(_enum(AgentStatus, "agent_status"), nullable=False, server_default="STOPPED")
    mode: Mapped[AgentMode | None] = mapped_column(_enum(AgentMode, "agent_mode"), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime] = _ts()
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the worker that ran the last cycle
    last_heartbeat_at: Mapped[datetime | None] = _ts(nullable=True)
    last_cycle_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentConfigVersion(UserOwnedMixin, Base):
    """An immutable configuration: universe, selection mode and candidates (the backtest request's JSON shape).
    A new version takes effect from `effective_from_session` (approved decision D12)."""

    __tablename__ = "agent_config_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_config_versions_agent_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("selection_mode IN ('REGIME_PRIORITY', 'PINNED')", name="selection_mode_valid"),
        CheckConstraint("warmup_sessions >= 0 AND warmup_sessions <= 20", name="warmup_sessions_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = _agent_id()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False)
    warmup_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from_session: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = _ts()


class AgentCycle(UserOwnedMixin, Base):
    __tablename__ = "agent_cycles"
    __table_args__ = (
        Index("uq_agent_cycles_one_running", "agent_id", unique=True, postgresql_where=text("status = 'RUNNING'")),
        CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name="finished_iff_not_running"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = _agent_id()
    status: Mapped[AgentCycleStatus] = mapped_column(_enum(AgentCycleStatus, "agent_cycle_status"), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_status: Mapped[AgentStatus] = mapped_column(_enum(AgentStatus, "agent_status"), nullable=False)  # at start
    mode: Mapped[AgentMode | None] = mapped_column(_enum(AgentMode, "agent_mode"), nullable=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentDecision(UserOwnedMixin, Base):
    """The deterministic journal of what the agent saw and did (append-only)."""

    __tablename__ = "agent_decisions"
    __table_args__ = (
        Index(
            "uq_agent_decisions_one_evaluation_per_bar", "agent_id", "instrument_id", "bar_start", unique=True,
            postgresql_where=text("kind = 'EVALUATION'"),
        ),
        Index(
            "uq_agent_decisions_one_selection_per_session", "agent_id", "instrument_id", "session_date", unique=True,
            postgresql_where=text("kind = 'SELECTION'"),
        ),
        Index(
            "uq_agent_decisions_client_order_id", "agent_id", "client_order_id", unique=True,
            postgresql_where=text("client_order_id IS NOT NULL"),
        ),
        Index("ix_agent_decisions_agent_decided", "agent_id", "decided_at"),
        CheckConstraint("(client_order_id IS NULL) = (order_id IS NULL)", name="order_iff_client_order_id"),
        CheckConstraint("kind = 'SELECTION' OR bar_start IS NOT NULL", name="evaluation_has_bar"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = _agent_id(index=False)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("agent_cycles.id", ondelete="RESTRICT"), nullable=False, index=True)
    config_version_id: Mapped[int] = mapped_column(ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[AgentDecisionKind] = mapped_column(_enum(AgentDecisionKind, "agent_decision_kind"), nullable=False)
    bar_start: Mapped[datetime | None] = _ts(nullable=True)
    candle_id: Mapped[int | None] = mapped_column(ForeignKey("candles.id", ondelete="RESTRICT"), nullable=True)
    observation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome: Mapped[AgentDecisionOutcome] = mapped_column(_enum(AgentDecisionOutcome, "agent_decision_outcome"), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    explanation: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    risk_decision_id: Mapped[int | None] = mapped_column(ForeignKey("risk_decisions.id", ondelete="RESTRICT"), nullable=True)
    order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the order's status when submitted
    decided_at: Mapped[datetime] = _ts()


class AgentEvent(UserOwnedMixin, Base):
    """Append-only operational history: transitions, kill switch, reconciliation, discrepancies, failures."""

    __tablename__ = "agent_events"
    __table_args__ = (
        CheckConstraint("actor IN ('USER', 'WORKER', 'SYSTEM')", name="actor_valid"),
        Index("ix_agent_events_agent_occurred", "agent_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = _agent_id(index=False)
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("agent_cycles.id", ondelete="RESTRICT"), nullable=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    occurred_at: Mapped[datetime] = _ts()


class AgentInstrumentState(UserOwnedMixin, Base):
    """Per-instrument working state: the processed-bar watermark, the replay anchor, an owed exit and any block.
    Everything here can be re-derived from the authoritative orders, fills and the archive; it only prevents
    repeated processing and records what the agent was waiting for."""

    __tablename__ = "agent_instrument_states"
    __table_args__ = (
        UniqueConstraint("agent_id", "instrument_id", name="uq_agent_instrument_states_agent_instrument"),
        CheckConstraint("(owed_exit_since IS NULL) = (owed_exit_session IS NULL)", name="owed_exit_complete"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = _agent_id(index=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[AgentInstrumentStatus] = mapped_column(
        _enum(AgentInstrumentStatus, "agent_instrument_status"), nullable=False, server_default="OK"
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_session: Mapped[date | None] = mapped_column(Date, nullable=True)  # the session a block was detected in
    last_bar_start: Mapped[datetime | None] = _ts(nullable=True)  # watermark: bars up to here are processed
    last_candle_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replay_session: Mapped[date | None] = mapped_column(Date, nullable=True)  # the session the anchor belongs to
    replay_from_session: Mapped[date | None] = mapped_column(Date, nullable=True)  # first warm-up session replayed
    owed_exit_since: Mapped[datetime | None] = _ts(nullable=True)  # bar whose exit signal is still owed
    owed_exit_session: Mapped[date | None] = mapped_column(Date, nullable=True)
    owed_exit_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
