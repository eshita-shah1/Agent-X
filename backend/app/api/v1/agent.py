"""Phase 5 endpoints: the account's autonomous agent (paper trading only).

Identity comes only from the authenticated user; another user's agent is never visible (404). State-changing calls
run in one transaction under the account lock, are recorded as agent events, and return the persisted state read
back after commit: an accepted request is not reported as a transition that did not happen. Orders are never placed
here: the worker submits them through OrderService.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.agent import configuration, lifecycle
from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.schemas_agent import (
    AgentConfigRequest,
    AgentConfigResponse,
    AgentConfigSaveResponse,
    AgentCycleResponse,
    AgentDecisionResponse,
    AgentEventResponse,
    AgentStatusResponse,
    InstrumentStateResponse,
    KillSwitchEngageRequest,
)
from app.api.v1.trading import get_clock
from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import AgentDecisionKind, AgentDecisionOutcome, AgentInstrumentStatus, AgentMode, AgentStatus
from app.db.models import (
    AgentCycle,
    AgentDecision,
    AgentEvent,
    AgentInstrumentState,
    ProtectiveExitRequirement,
    User,
)
from app.db.scoping import owned_select
from app.exchange.readiness import execution_readiness
from app.exchange.service import session_date_of
from app.marketdata.service import MarketDataService
from app.orders.service import TradingContext

router = APIRouter(prefix="/agent", tags=["agent"])


def _not_configured() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "No agent is configured (PUT /api/v1/agent/config)")


def _map(exc: lifecycle.AgentError) -> HTTPException:
    if isinstance(exc, lifecycle.AgentNotConfigured):
        return _not_configured()
    if isinstance(exc, lifecycle.AgentRefused):
        return HTTPException(status.HTTP_409_CONFLICT, "AGENT_REFUSED: " + "; ".join(exc.problems))
    return HTTPException(status.HTTP_409_CONFLICT, f"INVALID_TRANSITION: {exc}")


def build_status(db: Session, settings: Settings, now: datetime, user: User) -> AgentStatusResponse:
    agent = configuration.get_agent(db, user.id)
    readiness = execution_readiness(settings, session_date_of(now))
    risk_state = TradingContext.build(db, settings, lambda: now).risk.get_state(user.id, create=False)
    kill = bool(risk_state and risk_state.kill_switch_engaged)
    unresolved = len(db.scalars(
        owned_select(ProtectiveExitRequirement, user.id).where(ProtectiveExitRequirement.status == "OPEN")
    ).all())
    if agent is None:
        return AgentStatusResponse(
            configured=False, status=None, mode=None, status_reason=None, status_changed_at=None, entries_allowed=False,
            blockers=["NOT_CONFIGURED: no agent is configured"], disclosures=[], kill_switch_engaged=kill,
            execution_enabled=readiness.enabled, execution_blocking_reasons=readiness.blocking_reasons,
            effective_config=None, pending_config=None, instruments=[], last_heartbeat_at=None, worker_id=None,
            last_cycle_id=None, consecutive_failures=0, unresolved_protective_exits=unresolved,
        )
    effective = configuration.effective_version(db, agent, session_date_of(now))
    latest = configuration.latest_version(db, agent)
    pending = latest if latest is not None and (effective is None or latest.id != effective.id) else None
    states = db.scalars(
        owned_select(AgentInstrumentState, user.id).where(AgentInstrumentState.agent_id == agent.id)
        .order_by(AgentInstrumentState.instrument_id)
    ).all()
    blockers = []
    if agent.status is not AgentStatus.RUNNING:
        blockers.append(f"AGENT_{agent.status.value}: {agent.status_reason or ''}".rstrip(": "))
    if kill:
        blockers.append(f"KILL_SWITCH_ENGAGED: {risk_state.kill_switch_reason}")
    if agent.mode is AgentMode.SIGNALS_ONLY:
        blockers.append("SIGNALS_ONLY: the agent never submits orders in this mode")
    if not readiness.enabled:
        blockers += readiness.blocking_reasons
    if lifecycle.worker_stale(agent, now, settings.agent_worker_stale_after_seconds):
        blockers.append("WORKER_NOT_SEEN: no agent cycle has run recently (python -m app.agent.cli cycle)")
    blockers += [
        f"INSTRUMENT_{s.status.value}: {s.instrument_id}: {s.status_detail}" for s in states
        if s.status is not AgentInstrumentStatus.OK
    ]
    entries_allowed = (agent.status is AgentStatus.RUNNING and agent.mode is AgentMode.EXECUTION and not kill
                       and readiness.enabled)
    disclosures = ["Paper trading only; no real-money or broker execution exists."]
    if agent.mode is AgentMode.SIGNALS_ONLY or not readiness.enabled:
        disclosures.append(lifecycle.SIGNALS_ONLY_DISCLOSURE)
    return AgentStatusResponse(
        configured=True, status=agent.status, mode=agent.mode, status_reason=agent.status_reason,
        status_changed_at=agent.status_changed_at, entries_allowed=entries_allowed, blockers=blockers,
        disclosures=disclosures, kill_switch_engaged=kill, execution_enabled=readiness.enabled,
        execution_blocking_reasons=readiness.blocking_reasons,
        effective_config=AgentConfigResponse.model_validate(effective) if effective else None,
        pending_config=AgentConfigResponse.model_validate(pending) if pending else None,
        instruments=[InstrumentStateResponse.model_validate(s) for s in states],
        last_heartbeat_at=agent.last_heartbeat_at, worker_id=agent.worker_id, last_cycle_id=agent.last_cycle_id,
        consecutive_failures=agent.consecutive_failures, unresolved_protective_exits=unresolved,
    )


@router.get("/status", response_model=AgentStatusResponse)
def get_status(user: User = Depends(get_current_user), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    return build_status(db, settings, clock(), user)


@router.get("/config", response_model=AgentConfigResponse)
def get_config(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    agent = configuration.get_agent(db, user.id)
    latest = configuration.latest_version(db, agent) if agent else None
    if latest is None:
        raise _not_configured()
    return latest


@router.put("/config", response_model=AgentConfigSaveResponse, status_code=status.HTTP_201_CREATED)
def put_config(body: AgentConfigRequest, request: Request, response: Response, user: User = Depends(get_current_user),
               db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
               clock: Clock = Depends(get_clock)):
    svc = MarketDataService(db, request.app.state.provider_registry, settings, clock)
    data = configuration.AgentConfigInput(
        instrument_ids=body.instrument_ids, mode=body.mode,
        candidates=[c.model_dump() for c in body.candidates], warmup_sessions=body.warmup_sessions,
    )
    try:
        _agent, version, created = lifecycle.configure(db, user.id, data, svc.calendar, clock())
    except lifecycle.AgentRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(exc.problems)) from None
    except lifecycle.AgentError as exc:
        raise _map(exc) from None
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return AgentConfigSaveResponse(config=AgentConfigResponse.model_validate(version), created=created)


def _transition(db: Session, settings: Settings, clock: Clock, user: User, action) -> AgentStatusResponse:
    try:
        action()
    except lifecycle.AgentRefused as exc:
        db.rollback()
        raise _map(exc) from None
    except lifecycle.AgentError as exc:
        db.rollback()
        raise _map(exc) from None
    db.commit()
    return build_status(db, settings, clock(), user)


@router.post("/start", response_model=AgentStatusResponse)
def start(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    ctx = TradingContext.build(db, settings, clock)
    svc = MarketDataService(db, request.app.state.provider_registry, settings, clock)
    return _transition(db, settings, clock, user, lambda: lifecycle.start(db, settings, ctx, svc, user.id, clock()))


@router.post("/pause", response_model=AgentStatusResponse)
def pause(user: User = Depends(get_current_user), db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    return _transition(db, settings, clock, user, lambda: lifecycle.pause(db, user.id, clock()))


@router.post("/resume", response_model=AgentStatusResponse)
def resume(user: User = Depends(get_current_user), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    ctx = TradingContext.build(db, settings, clock)
    return _transition(db, settings, clock, user, lambda: lifecycle.resume(db, settings, ctx, user.id, clock()))


@router.post("/stop", response_model=AgentStatusResponse)
def stop(user: User = Depends(get_current_user), db: Session = Depends(get_db),
         settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    return _transition(db, settings, clock, user, lambda: lifecycle.stop(db, user.id, clock()))


@router.post("/kill-switch", response_model=AgentStatusResponse)
def kill_switch(body: KillSwitchEngageRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)):
    """Engages the Phase 4 kill switch and pauses the agent. Release it with POST /api/v1/risk/kill-switch; the agent
    then stays PAUSED until POST /api/v1/agent/resume."""
    ctx = TradingContext.build(db, settings, clock)
    return _transition(db, settings, clock, user,
                       lambda: lifecycle.engage_kill_switch(db, ctx, user.id, body.reason, clock()))


@router.get("/decisions", response_model=list[AgentDecisionResponse])
def list_decisions(
    instrument_id: int | None = Query(default=None, gt=0),
    kind: AgentDecisionKind | None = None,
    outcome: AgentDecisionOutcome | None = None,
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    q = owned_select(AgentDecision, user.id)
    if instrument_id is not None:
        q = q.where(AgentDecision.instrument_id == instrument_id)
    if kind is not None:
        q = q.where(AgentDecision.kind == kind)
    if outcome is not None:
        q = q.where(AgentDecision.outcome == outcome)
    if before_id is not None:
        q = q.where(AgentDecision.id < before_id)
    return db.scalars(q.order_by(AgentDecision.id.desc()).limit(limit)).all()


@router.get("/events", response_model=list[AgentEventResponse])
def list_events(
    before_id: int | None = Query(default=None, gt=0), limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    q = owned_select(AgentEvent, user.id)
    if before_id is not None:
        q = q.where(AgentEvent.id < before_id)
    return db.scalars(q.order_by(AgentEvent.id.desc()).limit(limit)).all()


@router.get("/cycles", response_model=list[AgentCycleResponse])
def list_cycles(
    before_id: int | None = Query(default=None, gt=0), limit: int = Query(default=50, ge=1, le=500),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    q = owned_select(AgentCycle, user.id)
    if before_id is not None:
        q = q.where(AgentCycle.id < before_id)
    return db.scalars(q.order_by(AgentCycle.id.desc()).limit(limit)).all()

