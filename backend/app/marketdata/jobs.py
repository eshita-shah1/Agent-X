"""Ingestion job orchestration: backfill and incremental runs, independently callable.

Concurrency: the database allows at most one QUEUED/RUNNING job (partial unique index), so two runs can never
interleave writes or double-hit the provider. A job whose heartbeat has stopped (crash/restart) is marked
INTERRUPTED by the next job request, which then proceeds. Safe to re-run: archiving is idempotent.

This is deliberately NOT a scheduler and starts nothing on application start-up: ingestion is operator-initiated
(approved for Phase 2). Scheduling may be reconsidered in a later phase; ingestion and any trading agent stay
decoupled.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.enums import BatchStatus, InstrumentType, JobStatus, JobType, Timeframe, TradingStatus
from app.db.models import DataIngestionJob, DataProvider, Instrument
from app.marketdata.archive import latest_canonical_bar_end
from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.errors import InstrumentDisabled, UnsupportedCapability
from app.marketdata.providers.base import ProviderDescriptor
from app.marketdata.providers.registry import ProviderRegistry
from app.marketdata.service import MarketDataService, utcnow
from app.marketdata.types import IssueType, QualityIssue, Severity

log = logging.getLogger("agentx.marketdata.jobs")
MAX_BACKFILL_DAYS = 60  # the approved target: the latest 60 calendar days of 5-minute data


def iso_utc(ts: datetime) -> str:
    """The API's UTC representation (`...Z`), for timestamps stored inside JSON payloads such as a job summary."""
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


class JobAlreadyRunning(Exception):
    def __init__(self, job_id):
        super().__init__(f"ingestion job {job_id} is already active")
        self.job_id = job_id


def lookback_floor(descriptor: ProviderDescriptor, timeframe: str, now: datetime) -> datetime:
    """Earliest request start the provider accepts, kept safely inside its measured limit."""
    limit = descriptor.max_lookback_by_interval.get(timeframe)
    if limit is None:
        raise UnsupportedCapability(f"{descriptor.code} has no verified lookback limit for {timeframe}", provider=descriptor.code)
    return now - limit + descriptor.lookback_safety


def clean_start(desired: datetime, floor: datetime, calendar) -> datetime:
    """Prefer the local midnight of the desired start's date: a request starting at or before the session open
    gets an intact first bar (measured), whereas a start inside a session gets a truncated one. If that midnight
    would fall before the provider's lookback floor, keep `desired` (the pipeline then discards the edge bar)."""
    start = max(desired, floor)
    day = calendar.local_date(start)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ).astimezone(UTC)
    return midnight if midnight >= floor else start


def backfill_window(descriptor: ProviderDescriptor, timeframe: str, days: int, now: datetime, calendar=None) -> tuple[datetime, datetime]:
    floor = lookback_floor(descriptor, timeframe, now)
    desired = max(now - timedelta(days=days), floor)
    return (clean_start(desired, floor, calendar) if calendar is not None else desired), now


def resolve_targets(service: MarketDataService, provider_code: str, instrument_ids: list[int] | None) -> list[Instrument]:
    """Explicit ids must be ingestable (a disabled/unmapped instrument is an error, never silently skipped);
    with no ids, every enabled equity with an explicit provider mapping is used."""
    provider = service.provider_row(provider_code)
    if instrument_ids:
        out = []
        for iid in dict.fromkeys(instrument_ids):
            inst = service.get_instrument(iid)
            service.provider_symbol(inst, provider)  # raises InstrumentDisabled
            out.append(inst)
        return out
    out = []
    for inst in service.list_instruments(instrument_type=InstrumentType.EQUITY, enabled=True):
        try:
            service.provider_symbol(inst, provider)
            out.append(inst)
        except InstrumentDisabled:
            continue
    return out


def create_job(
    session: Session, *, job_type: JobType, provider_code: str, timeframe: str, parameters: dict[str, Any],
    user_id: uuid.UUID | None, settings: Settings, clock=utcnow,
) -> DataIngestionJob:
    provider = session.scalar(select(DataProvider).where(DataProvider.code == provider_code))
    if provider is None:
        raise UnsupportedCapability(f"provider {provider_code!r} is not registered", provider=provider_code)
    now = clock()
    active = session.scalar(select(DataIngestionJob).where(DataIngestionJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
    if active is not None:
        beat = active.heartbeat_at or active.started_at or active.created_at
        if now - beat > timedelta(seconds=settings.marketdata_job_heartbeat_timeout_seconds):
            active.status, active.finished_at = JobStatus.INTERRUPTED, now
            active.error_message = "no heartbeat within the timeout; presumed crashed or restarted"
            session.commit()
        else:
            raise JobAlreadyRunning(active.id)
    job = DataIngestionJob(
        job_type=job_type, status=JobStatus.QUEUED, requested_by_user_id=user_id, provider_id=provider.id,
        timeframe=Timeframe(timeframe), parameters=parameters, heartbeat_at=now,
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError:  # lost a race to another creator
        session.rollback()
        other = session.scalar(select(DataIngestionJob).where(DataIngestionJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
        raise JobAlreadyRunning(other.id if other else None) from None
    return job


def _plan_window(service: MarketDataService, inst: Instrument, job: DataIngestionJob, provider, descriptor, now):
    """Returns (start, end, pre_issues) for one instrument."""
    tf = job.timeframe.value
    params = job.parameters
    pre: list[QualityIssue] = []
    if job.job_type is JobType.BACKFILL:
        start, end = backfill_window(descriptor, tf, int(params.get("days", MAX_BACKFILL_DAYS)), now, service.calendar)
        return start, end, pre
    floor = lookback_floor(descriptor, tf, now)
    last_end = latest_canonical_bar_end(service.session, inst.id, provider.id, job.timeframe)
    if last_end is None:
        return clean_start(now - timedelta(days=MAX_BACKFILL_DAYS), floor, service.calendar), now, pre
    # overlap => restatement check; starting at local midnight keeps the first returned bar intact
    start = clean_start(last_end - timedelta(hours=service.settings.marketdata_incremental_overlap_hours), floor, service.calendar)
    if last_end < floor:
        pre.append(
            QualityIssue(
                IssueType.UNRECOVERABLE_GAP, Severity.ERROR, last_end,
                "archive ends before the provider's lookback window; the interval in between cannot be recovered from this provider",
                {"archive_ends": iso_utc(last_end), "provider_earliest_start": iso_utc(floor)},
            )
        )
    return start, now, pre


def run_job(session_factory: sessionmaker[Session], registry: ProviderRegistry, settings: Settings, job_id, clock=utcnow, sleep=time.sleep) -> None:
    """Execute a job to completion. Never raises: every outcome is persisted on the job."""
    try:
        with session_factory() as s:
            job = s.get(DataIngestionJob, job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                return
            job.status, job.started_at, job.heartbeat_at = JobStatus.RUNNING, clock(), clock()
            s.commit()
            provider_code = s.get(DataProvider, job.provider_id).code
            instrument_ids = list(job.parameters.get("instrument_ids", []))
        results: list[dict[str, Any]] = []
        for idx, iid in enumerate(instrument_ids):
            if idx and settings.marketdata_request_pause_seconds:
                sleep(settings.marketdata_request_pause_seconds)  # be polite to the provider; avoids rate limits
            results.append(_run_one(session_factory, registry, settings, job_id, provider_code, iid, clock))
            with session_factory() as s:
                s.get(DataIngestionJob, job_id).heartbeat_at = clock()
                s.commit()
        failed = [r for r in results if r["status"] in ("FAILED", "SKIPPED_DISABLED")]
        if not results:
            status = JobStatus.FAILED
        elif len(failed) == len(results):
            status = JobStatus.FAILED
        elif failed:
            status = JobStatus.PARTIAL
        else:
            status = JobStatus.SUCCEEDED
        _finish(session_factory, job_id, status, None, {"instruments": results, "failed": len(failed), "total": len(results)}, clock)
    except Exception as exc:  # noqa: BLE001
        log.exception("ingestion job %s crashed", job_id)
        _finish(session_factory, job_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}"[:1000], {}, clock)


def _run_one(session_factory, registry, settings, job_id, provider_code, instrument_id, clock) -> dict[str, Any]:
    with session_factory() as s:  # a fresh session per instrument: one instrument's failure cannot poison the rest
        try:
            svc = MarketDataService(s, registry, settings, clock)
            job = s.get(DataIngestionJob, job_id)
            inst = svc.get_instrument(instrument_id)
            provider = svc.provider_row(provider_code)
            adapter = registry.get(provider_code)
            start, end, pre = _plan_window(svc, inst, job, provider, adapter.descriptor, clock())
            outcome = svc.ingest_window(
                job_id=job_id, instrument=inst, provider_code=provider_code, timeframe=job.timeframe.value, start=start, end=end,
                pre_issues=pre,
            )
            return {
                "instrument_id": inst.id, "symbol": inst.symbol, "status": outcome.status.value, "batch_id": outcome.batch_id,
                "rows_received": outcome.rows_received, "candles_inserted": outcome.candles_inserted,
                "duplicates_identical": outcome.duplicates_identical, "restatements": outcome.restatements,
                "quarantined": outcome.quarantined, "non_final_dropped": outcome.non_final_dropped,
                "corporate_actions_recorded": outcome.corporate_actions_recorded,
                "error_kind": outcome.error_kind, "error": outcome.error_message,
                "window_start": iso_utc(start), "window_end": iso_utc(end),
            }
        except InstrumentDisabled as exc:
            s.rollback()
            return {"instrument_id": instrument_id, "status": "SKIPPED_DISABLED", "error_kind": "INSTRUMENT_DISABLED", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            log.exception("instrument %s failed in job %s", instrument_id, job_id)
            return {"instrument_id": instrument_id, "status": "FAILED", "error_kind": "INTERNAL_ERROR", "error": f"{type(exc).__name__}: {exc}"[:500]}


def _finish(session_factory, job_id, status, error, summary, clock) -> None:
    with session_factory() as s:
        job = s.get(DataIngestionJob, job_id)
        if job is None:
            return
        job.status, job.finished_at, job.error_message, job.summary = status, clock(), error, summary
        s.commit()
