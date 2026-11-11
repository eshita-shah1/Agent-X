"""User-initiated backtest runs: validation, queueing, background execution and atomic persistence.

  * `validate_submission` performs every refusal check (instruments, period, configuration, data, corporate
    actions) synchronously, so the API can answer 422 before anything is queued.
  * `create_run` stores the run as QUEUED. The database allows one QUEUED/RUNNING run per user; a run whose
    heartbeat has expired is first marked INTERRUPTED. Other users are never affected.
  * `execute_run` is handed to FastAPI BackgroundTasks; nothing runs at start-up and there is no scheduler. It
    rebuilds the request from the stored JSON, checks the config fingerprint, reloads the data, runs the pure engine
    and writes every result row plus the SUCCEEDED status in ONE transaction, so a run has either complete results
    or none. It never raises: refusals and crashes are recorded as FAILED with nothing stored.

Account data is only read (initial capital); nothing here writes to an account or touches an order or exchange.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import (
    DISCLOSURES,
    ENGINE_VERSION,
    FILL_MODEL,
    PRICE_BASIS,
    SIZING_MODEL,
    BacktestRefused,
    BacktestRequest,
    BacktestResult,
    CapitalSource,
    RefusalCode,
    canonical,
    config_fingerprint,
    prepare_backtest,
    run_backtest,
)
from app.backtest.fingerprint import config_document
from app.backtest_runs.loader import load_instrument_data
from app.backtest_runs.persistence import result_rows, run_summary
from app.backtest_runs.requests import build_request
from app.core.config import Settings
from app.db.enums import BacktestRunStatus
from app.db.models import (
    BacktestEquityPoint,
    BacktestEvent,
    BacktestInstrumentResult,
    BacktestRun,
    BacktestSelectionDecision,
    BacktestTrade,
    User,
    VirtualAccount,
)
from app.db.scoping import owned_select
from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.service import load_calendar, utcnow

log = logging.getLogger("agentx.backtest")
ACTIVE = (BacktestRunStatus.QUEUED, BacktestRunStatus.RUNNING)


class BacktestAlreadyRunning(Exception):
    def __init__(self, run_id) -> None:
        super().__init__(f"backtest run {run_id} is already active for this user")
        self.run_id = run_id


def resolve_capital(session: Session, user: User, override: Decimal | None) -> tuple[Decimal, CapitalSource]:
    """The account's initial capital (read-only), or the request's explicit override."""
    if override is not None:
        return Decimal(override), CapitalSource.REQUEST_OVERRIDE
    account = session.scalar(owned_select(VirtualAccount, user.id))
    if account is None:
        raise BacktestRefused(RefusalCode.INVALID_CONFIGURATION, "no virtual account; supply capital explicitly")
    return Decimal(account.initial_capital), CapitalSource.ACCOUNT_INITIAL_CAPITAL


def load_data(session: Session, settings: Settings, request: BacktestRequest) -> dict[Any, Any]:
    return {
        iid: load_instrument_data(session, settings, iid, request.start, request.end, request.warmup_sessions)
        for iid in request.instrument_ids
    }


def validate_submission(session: Session, settings: Settings, request: BacktestRequest) -> None:
    """Every refusal check, with no simulation. Raises BacktestRefused."""
    prepare_backtest(request, load_data(session, settings, request), load_calendar(session))


def _expire_stale(session: Session, user_id: uuid.UUID, now: datetime, settings: Settings) -> None:
    active = session.scalar(owned_select(BacktestRun, user_id).where(BacktestRun.status.in_(ACTIVE)))
    if active is None:
        return
    beat = active.heartbeat_at or active.started_at or active.created_at
    if now - beat > timedelta(seconds=settings.backtest_job_heartbeat_timeout_seconds):
        active.status, active.finished_at = BacktestRunStatus.INTERRUPTED, now
        active.error_code, active.error_message = "INTERRUPTED", "no heartbeat within the timeout; presumed crashed or restarted"
        session.commit()
    else:
        raise BacktestAlreadyRunning(active.id)


def create_run(
    session: Session, user: User, submitted: dict[str, Any], request: BacktestRequest, settings: Settings,
    clock: Callable[[], datetime] = utcnow,
) -> BacktestRun:
    now = clock()
    _expire_stale(session, user.id, now, settings)
    run = BacktestRun(
        user_id=user.id, status=BacktestRunStatus.QUEUED,
        request={"submitted": submitted, "config": canonical(config_document(request))},
        instrument_ids=list(request.instrument_ids), requested_start=request.start, requested_end=request.end,
        selection_mode=request.policy.mode.value, capital=request.capital, capital_source=request.capital_source,
        cost_basis=request.cost_model.cost_basis, price_basis=PRICE_BASIS, engine_version=ENGINE_VERSION,
        fill_model=FILL_MODEL, sizing_model=SIZING_MODEL, config_fingerprint=config_fingerprint(request),
        disclosures=list(DISCLOSURES), heartbeat_at=now,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:  # lost a race with another request of the same user
        session.rollback()
        other = session.scalar(owned_select(BacktestRun, user.id).where(BacktestRun.status.in_(ACTIVE)))
        raise BacktestAlreadyRunning(other.id if other else None) from None
    return run


def _beat(session_factory, run_id, clock) -> None:
    with session_factory() as s:
        run = s.get(BacktestRun, run_id)
        if run is not None and run.status is BacktestRunStatus.RUNNING:
            run.heartbeat_at = clock()
            s.commit()


def _fail(session_factory, run_id, code: str, message: str, clock) -> None:
    with session_factory() as s:
        run = s.get(BacktestRun, run_id)
        if run is None or run.status not in ACTIVE:
            return  # already finished or interrupted: never overwrite a final state
        run.status, run.finished_at, run.error_code, run.error_message = BacktestRunStatus.FAILED, clock(), code, message[:2000]
        s.commit()


def _persist(session: Session, run: BacktestRun, result: BacktestResult, clock) -> None:
    rows = result_rows(run.id, run.user_id, result)
    for model, items in (
        (BacktestInstrumentResult, rows.instruments), (BacktestSelectionDecision, rows.decisions), (BacktestTrade, rows.trades),
        (BacktestEvent, rows.events), (BacktestEquityPoint, rows.equity),
    ):
        if items:
            session.execute(insert(model), items)
    run.status, run.finished_at, run.heartbeat_at = BacktestRunStatus.SUCCEEDED, clock(), clock()
    run.data_fingerprint, run.result_fingerprint = result.data_fingerprint, result.result_fingerprint
    run.summary = run_summary(result)


def execute_run(session_factory: sessionmaker[Session], settings: Settings, run_id: uuid.UUID, clock: Callable[[], datetime] = utcnow) -> None:
    """Run a queued backtest to completion. Never raises: every outcome is recorded on the run."""
    try:
        with session_factory() as s:
            run = s.get(BacktestRun, run_id)
            if run is None or run.status is not BacktestRunStatus.QUEUED:
                return
            run.status, run.started_at, run.heartbeat_at = BacktestRunStatus.RUNNING, clock(), clock()
            s.commit()
            submitted, capital, source, stored_print = run.request["submitted"], run.capital, run.capital_source, run.config_fingerprint
        request = build_request(submitted, capital, source)
        if config_fingerprint(request) != stored_print:
            _fail(session_factory, run_id, "CONFIG_FINGERPRINT_MISMATCH", "the rebuilt request does not match the accepted one", clock)
            return
        with session_factory() as s:
            data = load_data(s, settings, request)
            calendar = load_calendar(s)
        _beat(session_factory, run_id, clock)
        result = run_backtest(request, data, calendar)
        _beat(session_factory, run_id, clock)
        with session_factory() as s:
            run = s.scalar(select(BacktestRun).where(BacktestRun.id == run_id).with_for_update())  # blocks a concurrent interrupt
            if run is None or run.status is not BacktestRunStatus.RUNNING:
                return  # interrupted meanwhile: store nothing
            _persist(s, run, result, clock)
            s.commit()
    except BacktestRefused as exc:
        _fail(session_factory, run_id, exc.code.value, str(exc), clock)
    except Exception as exc:  # noqa: BLE001
        log.exception("backtest run %s crashed", run_id)
        _fail(session_factory, run_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", clock)


def today_ist(clock: Callable[[], datetime]) -> date:
    return clock().astimezone(EXCHANGE_TZ).date()
