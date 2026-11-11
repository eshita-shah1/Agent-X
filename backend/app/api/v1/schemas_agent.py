"""Request/response schemas for the Phase 5 agent endpoints. Database models are never returned directly."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import (
    AgentCycleStatus,
    AgentDecisionKind,
    AgentDecisionOutcome,
    AgentInstrumentStatus,
    AgentMode,
    AgentStatus,
)


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(min_length=1, max_length=64)
    strategy_id: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int | None = Field(default=None, ge=1)


class AgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_ids: list[int] = Field(min_length=1, max_length=50)
    mode: str = Field(pattern="^(REGIME_PRIORITY|PINNED)$")
    candidates: list[CandidateIn] = Field(min_length=1, max_length=20)
    warmup_sessions: int = Field(default=2, ge=0, le=20)  # the backtest default (decision D16)


class AgentConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    selection_mode: str
    instrument_ids: list[int]
    candidates: list[dict[str, Any]]
    warmup_sessions: int
    fingerprint: str
    effective_from_session: date
    created_at: datetime


class AgentConfigSaveResponse(BaseModel):
    config: AgentConfigResponse
    created: bool  # False: identical to the latest version, nothing stored


class KillSwitchEngageRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class InstrumentStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    status: AgentInstrumentStatus
    status_detail: str | None
    last_bar_start: datetime | None
    replay_from_session: date | None
    owed_exit_since: datetime | None
    owed_exit_reason: str | None


class AgentStatusResponse(BaseModel):
    configured: bool
    status: AgentStatus | None
    mode: AgentMode | None
    status_reason: str | None
    status_changed_at: datetime | None
    entries_allowed: bool  # RUNNING, EXECUTION mode, no kill switch, execution available
    blockers: list[str]
    disclosures: list[str]
    kill_switch_engaged: bool
    execution_enabled: bool
    execution_blocking_reasons: list[str]
    effective_config: AgentConfigResponse | None
    pending_config: AgentConfigResponse | None  # saved, effective from a later session
    instruments: list[InstrumentStateResponse]
    last_heartbeat_at: datetime | None
    worker_id: str | None
    last_cycle_id: int | None
    consecutive_failures: int
    unresolved_protective_exits: int


class AgentDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_id: int
    instrument_id: int
    session_date: date
    kind: AgentDecisionKind
    outcome: AgentDecisionOutcome
    bar_start: datetime | None
    candle_id: int | None
    observation_version: int | None
    provider_code: str | None
    price_basis: str | None
    config_id: str | None
    strategy_id: str | None
    strategy_version: str | None
    action: str | None
    reason: str | None
    detail: str | None
    values: dict[str, Any]
    explanation: dict[str, Any]
    client_order_id: str | None
    order_id: uuid.UUID | None
    risk_decision_id: int | None
    order_status: str | None
    decided_at: datetime


class AgentEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_id: int | None
    instrument_id: int | None
    event_type: str
    actor: str
    from_status: str | None
    to_status: str | None
    details: dict[str, Any]
    occurred_at: datetime


class AgentCycleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: AgentCycleStatus
    worker_id: str
    agent_status: AgentStatus
    mode: AgentMode | None
    session_date: date
    started_at: datetime
    finished_at: datetime | None
    summary: dict[str, Any]
    error_code: str | None
    error_message: str | None
