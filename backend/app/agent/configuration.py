"""Agent configuration versions (approved decision D12).

A configuration is the universe (instrument ids), the selection mode and the candidates, in exactly the JSON shape of
a backtest request, built by the shared `app.strategies.configs` (D13), so the agent can only run approved strategies
under the existing selector. Versions are immutable. A new version can be saved only while the agent is not RUNNING
and takes effect from the next session (immediately only while the agent has never been started).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import AgentStatus, InstrumentType, TradingStatus
from app.db.models import AgentConfigVersion, AgentEvent, AgentInstance, Instrument
from app.db.scoping import owned_select
from app.marketdata.calendar import TradingCalendar
from app.strategies.configs import ConfigurationError, build_policy

DEFAULT_WARMUP_SESSIONS = 2  # the backtest default (approved decision D16)
MAX_INSTRUMENTS = 50


class AgentConfigError(ValueError):
    """The submitted agent configuration is not usable."""


@dataclass(frozen=True)
class AgentConfigInput:
    instrument_ids: list[int]
    mode: str
    candidates: list[dict[str, Any]]
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS

    def canonical(self) -> dict[str, Any]:
        return {
            "instrument_ids": sorted(self.instrument_ids),
            "mode": self.mode,
            "candidates": sorted(
                ({"config_id": c["config_id"], "strategy_id": c["strategy_id"], "params": c.get("params") or {},
                  "priority": c.get("priority")} for c in self.candidates),
                key=lambda c: c["config_id"],
            ),
            "warmup_sessions": self.warmup_sessions,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, default=str).encode()).hexdigest()


def instrument_problem(db: Session, instrument_id: int) -> str | None:
    """Why an instrument cannot be traded by the agent (unknown, not an equity, disabled), or None."""
    inst = db.get(Instrument, instrument_id)
    if inst is None:
        return f"instrument {instrument_id}: unknown"
    if inst.instrument_type is not InstrumentType.EQUITY:
        return f"instrument {instrument_id} ({inst.symbol}): only equities are supported (futures are disabled)"
    if not inst.is_active or inst.trading_status is not TradingStatus.ENABLED:
        return f"instrument {instrument_id} ({inst.symbol}): disabled ({inst.disabled_reason})"
    return None


def validate(db: Session, data: AgentConfigInput) -> None:
    ids = data.instrument_ids
    if not ids:
        raise AgentConfigError("at least one instrument is required")
    if len(ids) > MAX_INSTRUMENTS:
        raise AgentConfigError(f"at most {MAX_INSTRUMENTS} instruments are allowed")
    if len(set(ids)) != len(ids):
        raise AgentConfigError("instrument_ids must be unique")
    if isinstance(data.warmup_sessions, bool) or not isinstance(data.warmup_sessions, int) or not 0 <= data.warmup_sessions <= 20:
        raise AgentConfigError("warmup_sessions must be an integer in 0..20")
    problems = [p for p in (instrument_problem(db, i) for i in ids) if p]
    if problems:
        raise AgentConfigError("; ".join(problems))
    try:
        build_policy(data.mode, data.candidates)
    except ConfigurationError as exc:
        raise AgentConfigError(str(exc)) from None


def policy_of(version: AgentConfigVersion):
    """The selection policy of a stored version (validated when it was saved)."""
    return build_policy(version.selection_mode, version.candidates)


def next_session(calendar: TradingCalendar, now: datetime) -> date:
    """Today's session if it has not opened yet, otherwise the next session after today."""
    today = calendar.local_date(now)
    session = calendar.session(today)
    if session is not None and now < session.open_ts:
        return today
    day = today + timedelta(days=1)
    for _ in range(30):
        if calendar.session(day) is not None:
            return day
        day += timedelta(days=1)
    raise RuntimeError(f"no trading session within 30 days after {today}")


def latest_version(db: Session, agent: AgentInstance) -> AgentConfigVersion | None:
    return db.scalar(
        owned_select(AgentConfigVersion, agent.user_id)
        .where(AgentConfigVersion.agent_id == agent.id)
        .order_by(AgentConfigVersion.version.desc())
        .limit(1)
    )


def effective_version(db: Session, agent: AgentInstance, session_date: date) -> AgentConfigVersion | None:
    """The configuration in force for a session: the latest version effective on or before it."""
    return db.scalar(
        owned_select(AgentConfigVersion, agent.user_id)
        .where(AgentConfigVersion.agent_id == agent.id, AgentConfigVersion.effective_from_session <= session_date)
        .order_by(AgentConfigVersion.version.desc())
        .limit(1)
    )


def ever_started(db: Session, agent: AgentInstance) -> bool:
    return bool(db.scalar(
        select(func.count()).select_from(AgentEvent).where(
            AgentEvent.user_id == agent.user_id, AgentEvent.agent_id == agent.id, AgentEvent.event_type == "agent.started"
        )
    ))


def save_version(
    db: Session, agent: AgentInstance, data: AgentConfigInput, calendar: TradingCalendar, now: datetime
) -> tuple[AgentConfigVersion, bool]:
    """Validate and store a new version. Returns (version, created); an identical resubmission returns the latest
    version unchanged. The caller holds the account lock and has checked that the agent is not RUNNING."""
    if agent.status is AgentStatus.RUNNING:
        raise RuntimeError("configuration changes require the agent not to be RUNNING")
    validate(db, data)
    fingerprint = data.fingerprint()
    latest = latest_version(db, agent)
    if latest is not None and latest.fingerprint == fingerprint:
        return latest, False
    canonical = data.canonical()
    effective = calendar.local_date(now) if latest is None or not ever_started(db, agent) else next_session(calendar, now)
    version = AgentConfigVersion(
        user_id=agent.user_id, agent_id=agent.id, version=(latest.version + 1) if latest else 1,
        selection_mode=canonical["mode"], instrument_ids=canonical["instrument_ids"], candidates=canonical["candidates"],
        warmup_sessions=canonical["warmup_sessions"], fingerprint=fingerprint, effective_from_session=effective,
        created_at=now,
    )
    db.add(version)
    db.flush()
    return version, True


def get_agent(db: Session, user_id: uuid.UUID, lock: bool = False) -> AgentInstance | None:
    q = owned_select(AgentInstance, user_id)
    return db.scalar(q.with_for_update() if lock else q)
