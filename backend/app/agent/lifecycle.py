"""Agent lifecycle: configuration, start, pause, resume, stop, the kill switch and reconciliation.

States (persisted, `agent_instances.status`; the trigger of migration 0012 enforces the same transitions):

    STOPPED --start--> RUNNING --pause--> PAUSED --resume--> RUNNING
    RUNNING | PAUSED | ERROR --stop--> STOPPED          RUNNING | PAUSED --(cycle failure, discrepancy)--> ERROR
    ERROR --resume--> RUNNING

  STOP   no strategy evaluation and no agent order; owed exits are abandoned (recorded). Nothing is liquidated, and
         the worker keeps running the Phase 4 duties (order processing, risk refresh, protective exits: decision D10).
  PAUSE  monitor mode: evaluation continues, strategy exits and owed exits are still submitted, entries are suppressed.
  KILL   the Phase 4 kill switch (`risk_state`), the only switch (D11): engaging it cancels every open order (Phase 4
         behaviour) and pauses the agent; releasing it (POST /risk/kill-switch) never resumes the agent.

Every operation runs in the caller's transaction under the user's account lock (`lock_account`), then the agent row
lock: the same order the worker uses, so a transition and a cycle never interleave. Nothing here commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import configuration
from app.agent.positions import AGENT_ORDER_PREFIX, assess
from app.core.config import Settings
from app.db.enums import (
    OPEN_ORDER_STATUSES,
    AgentInstrumentStatus,
    AgentMode,
    AgentStatus,
    OrderSource,
)
from app.db.models import (
    AgentConfigVersion,
    AgentDecision,
    AgentEvent,
    AgentInstance,
    AgentInstrumentState,
    DataProvider,
    Order,
)
from app.db.scoping import owned_select
from app.exchange.readiness import execution_readiness
from app.exchange.service import lock_account, session_date_of
from app.marketdata.calendar import TradingCalendar
from app.orders.service import TradingContext
from app.strategies.base import BAR

ALLOWED = {
    AgentStatus.STOPPED: {AgentStatus.RUNNING},
    AgentStatus.RUNNING: {AgentStatus.PAUSED, AgentStatus.STOPPED, AgentStatus.ERROR},
    AgentStatus.PAUSED: {AgentStatus.RUNNING, AgentStatus.STOPPED, AgentStatus.ERROR},
    AgentStatus.ERROR: {AgentStatus.RUNNING, AgentStatus.STOPPED},
}

SIGNALS_ONLY_DISCLOSURE = (
    "SIGNALS_ONLY: simulated execution was unavailable when the agent started. The agent evaluates the approved "
    "strategies and records their signals; it never submits an order, so nothing can fill."
)


class AgentError(Exception):
    pass


class AgentNotConfigured(AgentError):
    pass


class InvalidTransition(AgentError):
    pass


class AgentRefused(AgentError):
    """A start/resume/configuration request failed validation or reconciliation; nothing changed."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Reconciliation:
    discrepancies: list[str] = field(default_factory=list)
    instruments: dict[int, tuple[AgentInstrumentStatus, str | None]] = field(default_factory=dict)
    breaches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discrepancies": self.discrepancies,
            "instruments": {str(k): {"status": s.value, "detail": d} for k, (s, d) in self.instruments.items()},
            "risk_breaches": self.breaches,
        }


# ---- journal ----------------------------------------------------------------------------------------------------


def record_event(
    db: Session, agent: AgentInstance, event_type: str, actor: str, now: datetime, *, cycle_id: int | None = None,
    instrument_id: int | None = None, from_status: AgentStatus | None = None, to_status: AgentStatus | None = None,
    details: dict | None = None,
) -> AgentEvent:
    event = AgentEvent(
        user_id=agent.user_id, agent_id=agent.id, cycle_id=cycle_id, instrument_id=instrument_id, event_type=event_type,
        actor=actor, from_status=from_status.value if from_status else None,
        to_status=to_status.value if to_status else None, details=details or {}, occurred_at=now,
    )
    db.add(event)
    db.flush()
    return event


def transition(
    db: Session, agent: AgentInstance, to: AgentStatus, reason: str, actor: str, now: datetime, *,
    event_type: str, cycle_id: int | None = None, details: dict | None = None,
) -> None:
    if to not in ALLOWED[agent.status]:
        raise InvalidTransition(f"the agent is {agent.status.value}; it cannot become {to.value}")
    before = agent.status
    agent.status, agent.status_reason, agent.status_changed_at = to, reason, now
    if to is AgentStatus.STOPPED:
        agent.mode = None
    db.flush()
    record_event(db, agent, event_type, actor, now, cycle_id=cycle_id, from_status=before, to_status=to,
                 details={"reason": reason, **(details or {})})


# ---- configuration ----------------------------------------------------------------------------------------------


def configure(
    db: Session, user_id: uuid.UUID, data: configuration.AgentConfigInput, calendar: TradingCalendar, now: datetime
) -> tuple[AgentInstance, AgentConfigVersion, bool]:
    account = lock_account(db, user_id)
    agent = configuration.get_agent(db, user_id, lock=True)
    if agent is None:
        agent = AgentInstance(user_id=user_id, account_id=account.id, status=AgentStatus.STOPPED, status_changed_at=now)
        db.add(agent)
        db.flush()
        record_event(db, agent, "agent.created", "USER", now, to_status=AgentStatus.STOPPED)
    if agent.status is AgentStatus.RUNNING:
        raise InvalidTransition("the configuration can only change while the agent is not RUNNING (pause or stop it)")
    try:
        version, created = configuration.save_version(db, agent, data, calendar, now)
    except configuration.AgentConfigError as exc:
        raise AgentRefused([str(exc)]) from None
    if created:
        record_event(db, agent, "agent.config_saved", "USER", now, details={
            "version": version.version, "fingerprint": version.fingerprint,
            "effective_from_session": version.effective_from_session.isoformat(),
        })
    return agent, version, created


# ---- readiness and reconciliation -------------------------------------------------------------------------------


def _locked_agent(db: Session, user_id: uuid.UUID):
    account = lock_account(db, user_id)
    agent = configuration.get_agent(db, user_id, lock=True)
    if agent is None:
        raise AgentNotConfigured("no agent is configured for this account (PUT /api/v1/agent/config)")
    return account, agent


def discrepancies(db: Session, agent: AgentInstance, session_date: date) -> list[str]:
    """Records that contradict each other. Any of them blocks start/resume and puts a running agent in ERROR: the
    agent never creates compensating orders."""
    out = []
    unexplained = db.scalars(
        owned_select(Order, agent.user_id).where(
            Order.source == OrderSource.AGENT,
            ~select(AgentDecision.id).where(AgentDecision.order_id == Order.id).exists(),
        )
    ).all()
    out += [f"agent order {o.id} ({o.client_order_id}) has no agent decision" for o in unexplained]
    mismatched = db.execute(
        owned_select(AgentDecision, agent.user_id).add_columns(Order)
        .join(Order, Order.id == AgentDecision.order_id)
        .where(AgentDecision.agent_id == agent.id, Order.client_order_id != AgentDecision.client_order_id)
    ).all()
    out += [f"decision {d.id} references order {o.id} with another client order id" for d, o in mismatched]
    stale_open = db.scalars(
        owned_select(Order, agent.user_id).where(
            Order.source == OrderSource.AGENT, Order.status.in_(OPEN_ORDER_STATUSES)
        )
    ).all()
    out += [
        f"agent order {o.id} accepted {session_date_of(o.accepted_at)} is still open after processing"
        for o in stale_open if session_date_of(o.accepted_at) < session_date
    ]
    foreign = db.scalars(
        owned_select(Order, agent.user_id).where(
            Order.source != OrderSource.AGENT, Order.client_order_id.startswith(AGENT_ORDER_PREFIX)
        )
    ).all()
    out += [f"order {o.id} uses the agent's client order id prefix but is {o.source.value}" for o in foreign]
    return out


def reconcile(
    db: Session, ctx: TradingContext, account, agent: AgentInstance, version: AgentConfigVersion, now: datetime,
    process: bool = True,
) -> Reconciliation:
    """Bring Phase 4 state up to date (when `process`), then compare the agent's records with the authoritative
    orders, fills and positions. Nothing is corrected here: conflicts are reported."""
    if process:
        ctx.exchange.process_open_orders(account)
    risk = ctx.risk.refresh(account)
    today = session_date_of(now)
    rec = Reconciliation(discrepancies(db, agent, today), breaches=list(risk.breaches))
    for iid in version.instrument_ids:
        rec.instruments[iid] = assess(db, agent.user_id, iid, today)
    return rec


def _preflight(db: Session, settings: Settings, ctx: TradingContext, agent: AgentInstance, now: datetime,
               mode: AgentMode | None) -> tuple[AgentConfigVersion, AgentMode, list[str]]:
    """Validation shared by start and resume. Returns (configuration, mode, execution blockers)."""
    problems = []
    today = session_date_of(now)
    version = configuration.effective_version(db, agent, today)
    if version is None:
        raise AgentRefused(["no configuration is effective for today's session"])
    problems += [p for p in (configuration.instrument_problem(db, i) for i in version.instrument_ids) if p]
    if db.scalar(select(DataProvider.id).where(DataProvider.code == settings.marketdata_provider)) is None:
        problems.append(f"MARKET_DATA_UNAVAILABLE: provider {settings.marketdata_provider!r} is not registered (seed the registry)")
    try:
        configuration.policy_of(version)
    except ValueError as exc:
        problems.append(f"configuration {version.version}: {exc}")
    readiness = execution_readiness(settings, today)
    chosen = mode or (AgentMode.EXECUTION if readiness.enabled else AgentMode.SIGNALS_ONLY)
    if chosen is AgentMode.EXECUTION:
        if not readiness.enabled:
            problems += [f"execution is unavailable: {r}" for r in readiness.blocking_reasons]
        limits = ctx.risk.limits(agent.user_id)
        if limits is None:
            problems.append("RISK_SETTINGS_NOT_CONFIGURED: entries need saved risk settings")
        elif not limits.resize_allowed:
            problems.append("resize_allowed must be true: the agent proposes an upper-bound quantity that risk resizes (D4)")
    state = ctx.risk.get_state(agent.user_id, create=False)
    if state is not None and state.kill_switch_engaged:
        problems.append(f"KILL_SWITCH_ENGAGED: {state.kill_switch_reason} (release it with POST /api/v1/risk/kill-switch)")
    if problems:
        raise AgentRefused(problems)
    return version, chosen, readiness.blocking_reasons


def instrument_state(db: Session, agent: AgentInstance, instrument_id: int) -> AgentInstrumentState:
    state = db.scalar(
        owned_select(AgentInstrumentState, agent.user_id)
        .where(AgentInstrumentState.agent_id == agent.id, AgentInstrumentState.instrument_id == instrument_id)
        .with_for_update()
    )
    if state is None:
        state = AgentInstrumentState(user_id=agent.user_id, agent_id=agent.id, instrument_id=instrument_id,
                                     status=AgentInstrumentStatus.OK)
        db.add(state)
        db.flush()
    return state


def start(db: Session, settings: Settings, ctx: TradingContext, svc, user_id: uuid.UUID, now: datetime) -> AgentInstance:
    """Validate, reconcile, then RUNNING. The agent acts only on bars that finalize after it starts: each
    instrument's watermark is set to the latest archived bar."""
    account, agent = _locked_agent(db, user_id)
    if agent.status is not AgentStatus.STOPPED:
        raise InvalidTransition(f"the agent is {agent.status.value}; only a STOPPED agent can start")
    version, mode, blockers = _preflight(db, settings, ctx, agent, now, None)
    rec = reconcile(db, ctx, account, agent, version, now)
    if rec.discrepancies:
        record_event(db, agent, "agent.reconciliation_failed", "SYSTEM", now, details=rec.as_dict())
        raise AgentRefused([f"RECONCILIATION_FAILED: {d}" for d in rec.discrepancies])
    today = session_date_of(now)
    for iid in version.instrument_ids:
        state = instrument_state(db, agent, iid)
        fresh = svc.freshness(instrument_id=iid, timeframe="5m")
        latest_end = fresh["latest_finalized_bar_end"]
        state.last_bar_start = latest_end - BAR if latest_end is not None else None
        state.last_candle_id = None
        state.owed_exit_since = state.owed_exit_session = state.owed_exit_reason = None
        state.status, state.status_detail = rec.instruments[iid]
        state.status_session = today if state.status is not AgentInstrumentStatus.OK else None
    agent.mode = mode
    agent.consecutive_failures = 0
    record_event(db, agent, "agent.reconciled", "SYSTEM", now, details=rec.as_dict())
    transition(db, agent, AgentStatus.RUNNING, "started by the user", "USER", now, event_type="agent.started", details={
        "mode": mode.value, "config_version": version.version, "execution_blockers": blockers,
    })
    return agent


def pause(db: Session, user_id: uuid.UUID, now: datetime, reason: str = "paused by the user") -> AgentInstance:
    _account, agent = _locked_agent(db, user_id)
    if agent.status is not AgentStatus.RUNNING:
        raise InvalidTransition(f"the agent is {agent.status.value}; only a RUNNING agent can pause")
    transition(db, agent, AgentStatus.PAUSED, reason, "USER", now, event_type="agent.paused")
    return agent


def resume(db: Session, settings: Settings, ctx: TradingContext, user_id: uuid.UUID, now: datetime) -> AgentInstance:
    """Revalidate (same mode as at start), reconcile, then RUNNING. Entries resume only on bars after this point:
    signals recorded while paused are late (D5) and are never acted on."""
    account, agent = _locked_agent(db, user_id)
    if agent.status not in (AgentStatus.PAUSED, AgentStatus.ERROR):
        raise InvalidTransition(f"the agent is {agent.status.value}; only a PAUSED or ERROR agent can resume")
    if agent.mode is None:
        raise AgentRefused(["the agent has no mode; stop and start it"])
    version, mode, blockers = _preflight(db, settings, ctx, agent, now, agent.mode)
    rec = reconcile(db, ctx, account, agent, version, now)
    if rec.discrepancies:
        record_event(db, agent, "agent.reconciliation_failed", "SYSTEM", now, details=rec.as_dict())
        raise AgentRefused([f"RECONCILIATION_FAILED: {d}" for d in rec.discrepancies])
    agent.consecutive_failures = 0
    record_event(db, agent, "agent.reconciled", "SYSTEM", now, details=rec.as_dict())
    transition(db, agent, AgentStatus.RUNNING, "resumed by the user", "USER", now, event_type="agent.resumed",
               details={"mode": mode.value, "config_version": version.version, "execution_blockers": blockers})
    return agent


def stop(db: Session, user_id: uuid.UUID, now: datetime) -> AgentInstance:
    """No liquidation: positions and orders are untouched. Owed exits are abandoned (recorded per instrument);
    Phase 4 protective exits keep running for the account (D10)."""
    _account, agent = _locked_agent(db, user_id)
    if agent.status is AgentStatus.STOPPED:
        raise InvalidTransition("the agent is already STOPPED")
    for state in db.scalars(
        owned_select(AgentInstrumentState, agent.user_id)
        .where(AgentInstrumentState.agent_id == agent.id, AgentInstrumentState.owed_exit_since.is_not(None))
        .with_for_update()
    ):
        record_event(db, agent, "agent.owed_exit_abandoned_on_stop", "USER", now, instrument_id=state.instrument_id,
                     details={"owed_exit_since": state.owed_exit_since.isoformat(), "reason": state.owed_exit_reason})
        state.owed_exit_since = state.owed_exit_session = state.owed_exit_reason = None
    transition(db, agent, AgentStatus.STOPPED, "stopped by the user", "USER", now, event_type="agent.stopped")
    return agent


def engage_kill_switch(db: Session, ctx: TradingContext, user_id: uuid.UUID, reason: str, now: datetime) -> AgentInstance:
    """The Phase 4 kill switch (single source of truth): blocks entries and cancels every open order (Phase 4
    behaviour), then pauses a RUNNING agent. Exits stay possible; exits it cancelled are re-submitted as owed exits."""
    account, agent = _locked_agent(db, user_id)
    state = ctx.risk.set_kill_switch(account, True, reason, ctx.exchange)
    if agent.status is AgentStatus.RUNNING:
        transition(db, agent, AgentStatus.PAUSED, f"KILL_SWITCH_ENGAGED: {reason}", "USER", now,
                   event_type="agent.paused_by_kill_switch")
    record_event(db, agent, "agent.kill_switch_engaged", "USER", now, details={
        "reason": reason, "kill_switch_changed_at": state.kill_switch_changed_at.isoformat() if state.kill_switch_changed_at else None,
    })
    return agent


def worker_stale(agent: AgentInstance, now: datetime, stale_after_seconds: int) -> bool:
    if agent.status not in (AgentStatus.RUNNING, AgentStatus.PAUSED):
        return False
    return agent.last_heartbeat_at is None or now - agent.last_heartbeat_at > timedelta(seconds=stale_after_seconds)
