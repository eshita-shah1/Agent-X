"""Authenticated backtest endpoints (Phase 3, Step 4B).

Runs are user-owned: every query is scoped to the authenticated user and another user's run is a 404 (its existence
is not revealed). Results are read-only; there is no update or delete endpoint, and nothing ranks configurations by
results or activates a strategy (decision D15). Backtests are started only by an explicit request.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.schemas_backtest import BacktestCreate, BacktestRunOut, BacktestRunPage, ItemPage
from app.backtest import DEFAULT_WARMUP_SESSIONS, DISCLOSURES, ENGINE_VERSION, FILL_MODEL, PRICE_BASIS, SIZING_MODEL, BacktestRefused, RefusalCode
from app.backtest_runs.jobs import BacktestAlreadyRunning, create_run, execute_run, resolve_capital, today_ist, validate_submission
from app.backtest_runs.persistence import EQUITY_FIELDS, TRADE_FIELDS, row_out
from app.backtest_runs.requests import build_request
from app.core.config import Settings
from app.db.models import (
    BacktestEquityPoint,
    BacktestEvent,
    BacktestInstrumentResult,
    BacktestRun,
    BacktestSelectionDecision,
    BacktestTrade,
    User,
)
from app.db.scoping import owned_select
from app.strategies import DEFAULTS_NOTE, DEFAULTS_STATUS, STRATEGIES
from app.strategies.selection import RegimeConfig

router = APIRouter(prefix="/backtests", tags=["backtests"])

MAX_PERIOD_DAYS = 366
MAX_ROWS_PAGE = 500
MAX_EQUITY_PAGE = 5000


def _refused(exc: BacktestRefused) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": exc.code.value, "message": exc.message, "instrument_id": exc.instrument_id},
    )


def _run_out(run: BacktestRun) -> BacktestRunOut:
    return BacktestRunOut(
        id=run.id, status=run.status.value, instrument_ids=run.instrument_ids, requested_start=run.requested_start,
        requested_end=run.requested_end, selection_mode=run.selection_mode, capital=run.capital,
        capital_source=run.capital_source.value, cost_basis=run.cost_basis.value, price_basis=run.price_basis,
        engine_version=run.engine_version, fill_model=run.fill_model, sizing_model=run.sizing_model,
        config_fingerprint=run.config_fingerprint, data_fingerprint=run.data_fingerprint, result_fingerprint=run.result_fingerprint,
        request=run.request, disclosures=run.disclosures, summary=run.summary, error_code=run.error_code,
        error_message=run.error_message, created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
    )


def _own_run(db: Session, user: User, run_id: uuid.UUID) -> BacktestRun:
    run = db.scalar(owned_select(BacktestRun, user.id).where(BacktestRun.id == run_id))
    if run is None:  # 404, not 403: never reveal another user's run
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backtest not found")
    return run


def _own_instrument(db: Session, user: User, run_id: uuid.UUID, instrument_id: int) -> BacktestRun:
    run = _own_run(db, user, run_id)
    if instrument_id not in run.instrument_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instrument not part of this backtest")
    return run


# ---- strategy registry ---------------------------------------------------------------------------------------------


def _param_value(v: Any) -> Any:
    return v.isoformat(timespec="minutes") if isinstance(v, time) else v


@router.get("/strategies")
def list_strategies(_: User = Depends(get_current_user)) -> dict[str, Any]:
    """The strategy catalogue with approved initial defaults, and the fixed engine models. Static: no results."""
    regime = RegimeConfig()
    return {
        "strategies": [
            {
                "strategy_id": d.strategy_id, "version": d.version, "family": d.family.value, "description": d.description,
                "defaults": {k: _param_value(v) for k, v in d.defaults().items()},
                "parameters": [f.name for f in fields(d.config_type)],
            }
            for d in STRATEGIES.values()
        ],
        "defaults_status": DEFAULTS_STATUS,
        "defaults_note": DEFAULTS_NOTE,
        "selection_modes": ["REGIME_PRIORITY", "PINNED"],
        "regime": {
            "adx_period": regime.adx_period, "trend_min": regime.trend_min, "range_max": regime.range_max,
            "checkpoint": regime.checkpoint.isoformat(timespec="minutes"),
        },
        "engine": {
            "engine_version": ENGINE_VERSION, "fill_model": FILL_MODEL, "sizing_model": SIZING_MODEL, "price_basis": PRICE_BASIS,
            "default_warmup_sessions": DEFAULT_WARMUP_SESSIONS, "default_cost_basis": "GROSS_OF_COSTS",
        },
        "disclosures": list(DISCLOSURES),
    }


# ---- runs ------------------------------------------------------------------------------------------------------------


@router.post("", response_model=BacktestRunOut, status_code=status.HTTP_202_ACCEPTED)
def start_backtest(
    body: BacktestCreate, request: Request, background: BackgroundTasks, user: User = Depends(get_current_user),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
):
    clock = request.app.state.clock
    submitted = body.model_dump(mode="json", exclude={"capital"})
    try:
        if body.end > today_ist(clock):
            raise BacktestRefused(RefusalCode.INVALID_PERIOD, f"end {body.end} is in the future")
        if (body.end - body.start).days > MAX_PERIOD_DAYS:
            raise BacktestRefused(RefusalCode.INVALID_PERIOD, f"the period may span at most {MAX_PERIOD_DAYS} days")
        capital, source = resolve_capital(db, user, body.capital)
        backtest_request = build_request(submitted, capital, source)
        validate_submission(db, settings, backtest_request)  # every refusal before anything is queued
    except BacktestRefused as exc:
        raise _refused(exc) from None
    try:
        run = create_run(db, user, submitted, backtest_request, settings, clock)
    except BacktestAlreadyRunning as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "BACKTEST_ALREADY_RUNNING", "message": str(exc), "active_run_id": None if exc.run_id is None else str(exc.run_id)},
        ) from None
    background.add_task(execute_run, request.app.state.sessionmaker, settings, run.id, clock)
    return _run_out(run)


@router.get("", response_model=BacktestRunPage)
def list_backtests(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(BacktestRun).where(BacktestRun.user_id == user.id))
    rows = db.scalars(owned_select(BacktestRun, user.id).order_by(BacktestRun.created_at.desc(), BacktestRun.id).limit(limit).offset(offset)).all()
    return BacktestRunPage(items=[_run_out(r) for r in rows], count=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=BacktestRunOut)
def get_backtest(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _run_out(_own_run(db, user, run_id))


@router.get("/{run_id}/instruments")
def get_instrument_results(run_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = _own_run(db, user, run_id)
    rows = db.scalars(
        owned_select(BacktestInstrumentResult, user.id).where(BacktestInstrumentResult.run_id == run.id).order_by(BacktestInstrumentResult.instrument_id)
    ).all()
    names = ("instrument_id", "data_quality", "benchmark_status", "benchmark", "summary", "final_equity", "benchmark_final_value")
    return {"run_id": str(run.id), "status": run.status.value, "items": [row_out(r, names) for r in rows]}


def _page(db: Session, user: User, model, run_id: uuid.UUID, instrument_id: int, order, names, limit: int, offset: int) -> ItemPage:
    where = (model.user_id == user.id, model.run_id == run_id, model.instrument_id == instrument_id)
    total = db.scalar(select(func.count()).select_from(model).where(*where))
    rows = db.scalars(owned_select(model, user.id).where(*where[1:]).order_by(*order).limit(limit).offset(offset)).all()
    return ItemPage(items=[row_out(r, names) for r in rows], count=total, limit=limit, offset=offset)


@router.get("/{run_id}/instruments/{instrument_id}/trades", response_model=ItemPage)
def get_trades(
    run_id: uuid.UUID, instrument_id: int, limit: int = Query(100, ge=1, le=MAX_ROWS_PAGE), offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _own_instrument(db, user, run_id, instrument_id)
    return _page(db, user, BacktestTrade, run_id, instrument_id, (BacktestTrade.seq,), TRADE_FIELDS, limit, offset)


@router.get("/{run_id}/instruments/{instrument_id}/equity", response_model=ItemPage)
def get_equity(
    run_id: uuid.UUID, instrument_id: int, limit: int = Query(1000, ge=1, le=MAX_EQUITY_PAGE), offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _own_instrument(db, user, run_id, instrument_id)
    return _page(db, user, BacktestEquityPoint, run_id, instrument_id, (BacktestEquityPoint.bar_start,), EQUITY_FIELDS, limit, offset)


@router.get("/{run_id}/instruments/{instrument_id}/selections", response_model=ItemPage)
def get_selections(
    run_id: uuid.UUID, instrument_id: int, limit: int = Query(100, ge=1, le=MAX_ROWS_PAGE), offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _own_instrument(db, user, run_id, instrument_id)
    names = ("session_date", "status", "selected_config_id", "decision")
    return _page(db, user, BacktestSelectionDecision, run_id, instrument_id, (BacktestSelectionDecision.session_date,), names, limit, offset)


@router.get("/{run_id}/instruments/{instrument_id}/events", response_model=ItemPage)
def get_events(
    run_id: uuid.UUID, instrument_id: int, limit: int = Query(100, ge=1, le=MAX_ROWS_PAGE), offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _own_instrument(db, user, run_id, instrument_id)
    names = ("seq", "bar_start", "session_date", "event_type", "config_id", "detail")
    return _page(db, user, BacktestEvent, run_id, instrument_id, (BacktestEvent.seq,), names, limit, offset)
