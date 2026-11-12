"""Authenticated market-data endpoints (Phase 2). Market data itself is global; ingestion jobs are shown only to
the user who requested them. No endpoint accepts a user id; no endpoint exposes account data."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.operator import require_operator
from app.api.v1.schemas_market import (
    BackfillRequest,
    CandleOut,
    CandlePage,
    IncrementalRequest,
    InstrumentOut,
    JobOut,
    JobPage,
    ProviderSymbolOut,
)
from app.core.config import Settings
from app.db.enums import InstrumentType, JobType, Timeframe, TradingStatus
from app.db.models import DataIngestionJob, DataProvider, Instrument, InstrumentProviderSymbol, User
from app.marketdata.calendar import EXCHANGE_TZ, timeframe_minutes
from app.marketdata.errors import InstrumentDisabled, UnsupportedCapability
from app.marketdata.freshness import worst
from app.marketdata.jobs import JobAlreadyRunning, create_job, resolve_targets, run_job
from app.marketdata.providers.registry import ProviderRegistry
from app.marketdata.service import InstrumentNotFound, MarketDataService
from app.marketdata.types import DataStatus

router = APIRouter(prefix="/market", tags=["market-data"])

MAX_CANDLE_PAGE = 2000  # operational bound on rows per response
MAX_CANDLE_RANGE = timedelta(days=366)  # operational bound on the requested time range
MAX_COVERAGE_DAYS = 400
INGEST_TIMEFRAME = "5m"


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def get_service(
    request: Request, db: Session = Depends(get_db), registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings_dep),
) -> MarketDataService:
    return MarketDataService(db, registry, settings, request.app.state.clock)


def _aware(name: str, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(422, f"{name} must include a UTC offset (e.g. 2026-11-11T09:15:00+05:30); naive timestamps are not accepted")
    return value.astimezone(UTC)


def _instrument_or_404(svc: MarketDataService, instrument_id: int) -> Instrument:
    try:
        return svc.get_instrument(instrument_id)
    except InstrumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instrument not found") from None


def _disabled_409(exc: InstrumentDisabled) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": "INSTRUMENT_DISABLED", "message": str(exc)})


def _provider_503(exc: UnsupportedCapability) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "PROVIDER_NOT_CONFIGURED", "message": str(exc)})


def _instrument_out(svc: MarketDataService, inst: Instrument) -> InstrumentOut:
    rows = svc.session.execute(
        select(DataProvider.code, InstrumentProviderSymbol.provider_symbol, InstrumentProviderSymbol.verification_status)
        .join(DataProvider, DataProvider.id == InstrumentProviderSymbol.provider_id)
        .where(InstrumentProviderSymbol.instrument_id == inst.id)
    ).all()
    return InstrumentOut(
        id=inst.id, symbol=inst.symbol, exchange=inst.exchange, instrument_type=inst.instrument_type.value, name=inst.name,
        currency=inst.currency, timezone=inst.timezone, tick_size=inst.tick_size, lot_size=inst.lot_size, is_active=inst.is_active,
        trading_status=inst.trading_status.value, disabled_reason=inst.disabled_reason, contract_identifier=inst.contract_identifier,
        expiry_date=inst.expiry_date,
        provider_symbols=[ProviderSymbolOut(provider=c, provider_symbol=s, verification_status=v.value) for c, s, v in rows],
    )


# ---- instruments ---------------------------------------------------------------------------


@router.get("/instruments", response_model=list[InstrumentOut])
def list_instruments(
    instrument_type: InstrumentType | None = Query(None, alias="type"),
    enabled: bool | None = None,
    _: User = Depends(get_current_user),
    svc: MarketDataService = Depends(get_service),
):
    return [_instrument_out(svc, i) for i in svc.list_instruments(instrument_type=instrument_type, enabled=enabled)]


@router.get("/instruments/{instrument_id}", response_model=InstrumentOut)
def get_instrument(instrument_id: int, _: User = Depends(get_current_user), svc: MarketDataService = Depends(get_service)):
    return _instrument_out(svc, _instrument_or_404(svc, instrument_id))


# ---- candles and coverage -----------------------------------------------------------------


@router.get("/candles", response_model=CandlePage)
def get_candles(
    instrument_id: int,
    start: datetime,
    end: datetime,
    timeframe: Timeframe = Timeframe.M5,
    limit: int = Query(500, ge=1, le=MAX_CANDLE_PAGE),
    after: datetime | None = None,
    series: str = Query("raw", pattern="^(raw|adjusted)$"),
    _: User = Depends(get_current_user),
    svc: MarketDataService = Depends(get_service),
):
    start, end, after = _aware("start", start), _aware("end", end), _aware("after", after)
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if end - start > MAX_CANDLE_RANGE:
        raise HTTPException(422, f"range too large; at most {MAX_CANDLE_RANGE.days} days per request")
    _instrument_or_404(svc, instrument_id)
    try:
        rows, nxt = svc.get_candles(instrument_id=instrument_id, timeframe=timeframe.value, start=start, end=end, limit=limit, after=after, series=series)
    except InstrumentDisabled as exc:
        raise _disabled_409(exc) from None
    except UnsupportedCapability as exc:
        raise _provider_503(exc) from None
    return CandlePage(items=[CandleOut(**r) for r in rows], count=len(rows), next_cursor=nxt)


@router.get("/coverage")
def get_coverage(
    instrument_id: int,
    timeframe: Timeframe = Timeframe.M5,
    days: int = Query(60, ge=1, le=MAX_COVERAGE_DAYS),
    end_date: date | None = None,
    _: User = Depends(get_current_user),
    svc: MarketDataService = Depends(get_service),
) -> dict[str, Any]:
    try:
        timeframe_minutes(timeframe.value)
    except ValueError:
        raise HTTPException(422, f"coverage needs a fixed intraday bar length; timeframe {timeframe.value!r} is not supported") from None
    inst = _instrument_or_404(svc, instrument_id)
    if inst.trading_status is not TradingStatus.ENABLED:
        raise _disabled_409(InstrumentDisabled(f"{inst.symbol} {inst.instrument_type.value} is disabled: {inst.disabled_reason}"))
    end_day = end_date or svc.clock().astimezone(EXCHANGE_TZ).date()
    try:
        return svc.coverage(instrument_id=instrument_id, timeframe=timeframe.value, start_day=end_day - timedelta(days=days), end_day=end_day)
    except UnsupportedCapability as exc:
        raise _provider_503(exc) from None


# ---- ingestion -------------------------------------------------------------------------------


def _start_job(
    job_type: JobType, instrument_ids: list[int] | None, days: int, user: User, background: BackgroundTasks,
    request: Request, svc: MarketDataService, settings: Settings,
) -> JobOut:
    provider_code = settings.marketdata_provider
    try:
        targets = resolve_targets(svc, provider_code, instrument_ids)
    except InstrumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instrument not found") from None
    except InstrumentDisabled as exc:
        raise _disabled_409(exc) from None
    except UnsupportedCapability as exc:
        raise _provider_503(exc) from None
    if not targets:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "NO_INGESTABLE_INSTRUMENTS", "message": "no enabled instruments with a provider mapping; run the seed command"})
    try:
        job = create_job(
            svc.session, job_type=job_type, provider_code=provider_code, timeframe=INGEST_TIMEFRAME,
            parameters={"instrument_ids": [t.id for t in targets], "days": days, "source": "api"},
            user_id=user.id, settings=settings, clock=svc.clock,
        )
    except JobAlreadyRunning as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "INGESTION_ALREADY_RUNNING", "message": str(exc), "active_job_id": str(exc.job_id)}) from None
    background.add_task(run_job, request.app.state.sessionmaker, request.app.state.provider_registry, settings, job.id, svc.clock)
    return _job_out(job)


def ingestion_user(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings_dep)) -> User:
    """Who may start ingestion: any authenticated user in development/test (the approved Phase 2 local policy), an
    operator in staging/production (Phase 8; INGESTION_REQUIRES_OPERATOR overrides). Refusals are audited."""
    if settings.ingestion_operator_only:
        return require_operator(request, user, db)
    return user


@router.post("/ingestion/backfill", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def start_backfill(
    body: BackfillRequest, request: Request, background: BackgroundTasks, user: User = Depends(ingestion_user),
    svc: MarketDataService = Depends(get_service), settings: Settings = Depends(get_settings_dep),
):
    return _start_job(JobType.BACKFILL, body.instrument_ids, body.days, user, background, request, svc, settings)


@router.post("/ingestion/incremental", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def start_incremental(
    body: IncrementalRequest, request: Request, background: BackgroundTasks, user: User = Depends(ingestion_user),
    svc: MarketDataService = Depends(get_service), settings: Settings = Depends(get_settings_dep),
):
    return _start_job(JobType.INCREMENTAL, body.instrument_ids, 60, user, background, request, svc, settings)


def _job_out(job: DataIngestionJob) -> JobOut:
    return JobOut(
        id=job.id, job_type=job.job_type.value, status=job.status.value, timeframe=job.timeframe.value, parameters=job.parameters,
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at, heartbeat_at=job.heartbeat_at,
        error_message=job.error_message, summary=job.summary,
    )


@router.get("/ingestion/jobs", response_model=JobPage)
def list_jobs(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    # Only the caller's own jobs. Jobs started from the CLI have no user and are not listed here.
    where = DataIngestionJob.requested_by_user_id == user.id
    total = db.scalar(select(func.count()).select_from(DataIngestionJob).where(where))
    rows = db.scalars(select(DataIngestionJob).where(where).order_by(DataIngestionJob.created_at.desc()).limit(limit).offset(offset)).all()
    return JobPage(items=[_job_out(j) for j in rows], count=total, limit=limit, offset=offset)


@router.get("/ingestion/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = db.scalar(select(DataIngestionJob).where(DataIngestionJob.id == job_id, DataIngestionJob.requested_by_user_id == user.id))
    if job is None:  # 404, not 403: do not reveal that another user's job exists
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return _job_out(job)


# ---- providers -------------------------------------------------------------------------------


@router.get("/providers")
def list_providers(
    _: User = Depends(get_current_user), db: Session = Depends(get_db), registry: ProviderRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    configured = set(registry.codes())
    rows = db.scalars(select(DataProvider).order_by(DataProvider.code)).all()
    return {
        "active_provider": settings.marketdata_provider,
        "providers": [
            {"code": p.code, "name": p.name, "notes": p.notes, "is_active": p.is_active, "configured": p.code in configured}
            for p in rows
        ],
        "note": "There is no automatic provider fallback: ingestion uses only the configured active provider.",
    }


@router.get("/providers/health")
def providers_health(
    _: User = Depends(get_current_user), svc: MarketDataService = Depends(get_service), settings: Settings = Depends(get_settings_dep)
) -> dict[str, Any]:
    code = settings.marketdata_provider
    health = svc.provider_health(code)
    instruments = []
    for inst in svc.list_instruments(instrument_type=InstrumentType.EQUITY, enabled=True):
        instruments.append(svc.freshness(instrument_id=inst.id, timeframe=INGEST_TIMEFRAME))
    overall = worst(health.status, *[DataStatus(i["status"]) for i in instruments]) if instruments else health.status
    return {
        "provider": code,
        "provider_status": health.status.value,
        "provider_status_reason": health.reason,
        "last_attempt_at": health.last_attempt_at, "last_success_at": health.last_success_at, "last_failure_at": health.last_failure_at,
        "last_error_kind": health.last_error_kind, "last_error_message": health.last_error_message,
        "consecutive_failures": health.consecutive_failures,
        "last_observed_market_ts": health.last_observed_market_ts, "last_successful_ingestion_at": health.last_successful_ingestion_at,
        "overall_status": overall.value,
        "instruments": instruments,
        "thresholds": svc.thresholds_report(),
        "evaluated_at": svc.clock(),
    }


@router.get("/providers/capabilities")
def providers_capabilities(
    provider: str | None = None, _: User = Depends(get_current_user), svc: MarketDataService = Depends(get_service),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    code = provider or settings.marketdata_provider
    try:
        caps = svc.capabilities(code)
    except UnsupportedCapability:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found") from None
    return {
        "provider": code,
        "capabilities": caps,
        "legend": {
            "DOCUMENTED": "stated in documentation, not tested here",
            "VERIFIED": "demonstrated by a probe run (see evidence)",
            "UNSUPPORTED": "a probe showed it is not offered",
            "UNKNOWN": "nothing established",
        },
    }
