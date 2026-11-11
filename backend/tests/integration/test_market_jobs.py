"""Backfill / incremental job orchestration: windows, locking, isolation, recovery."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.enums import JobStatus, JobType, Timeframe
from app.db.models import Candle, DataIngestionBatch, DataIngestionJob, DataProvider, DataQualityIssue, Instrument
from app.db.session import make_sessionmaker
from app.marketdata import jobs as jobs_module
from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.errors import InstrumentDisabled, ProviderUnavailable, UnsupportedCapability
from app.marketdata.jobs import (
    JobAlreadyRunning,
    backfill_window,
    clean_start,
    create_job,
    iso_utc,
    lookback_floor,
    resolve_targets,
    run_job,
)
from app.marketdata.service import InstrumentNotFound, MarketDataService
from app.marketdata.types import IssueType, RawBar
from tests.support import CAL, NOW, WEEKDAYS, day_bars, days_bars

NO_SLEEP = lambda s: None  # noqa: E731


def ist(day, h, m, month=11):
    return datetime(2026, month, day, h, m, tzinfo=EXCHANGE_TZ)


@pytest.fixture
def sf(engine):
    return make_sessionmaker(engine)


def all_ids(svc):
    return [i.id for i in resolve_targets(svc, "yahoo", None)]


def start_job(db, svc, md_settings, clock, job_type=JobType.BACKFILL, ids=None, days=60):
    return create_job(
        db, job_type=job_type, provider_code="yahoo", timeframe="5m", user_id=None, settings=md_settings, clock=clock,
        parameters={"instrument_ids": ids if ids is not None else all_ids(svc), "days": days},
    )


def run(db, svc, sf, registry, md_settings, clock, job_type=JobType.BACKFILL, ids=None, days=60, sleep=NO_SLEEP, settings=None):
    job = start_job(db, svc, md_settings, clock, job_type, ids, days)
    run_job(sf, registry, settings or md_settings, job.id, clock=clock, sleep=sleep)
    db.expire_all()
    return db.get(DataIngestionJob, job.id)


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


# ---- windows -----------------------------------------------------------------------------------------


def test_the_lookback_floor_sits_just_inside_the_providers_limit(registry):
    d = registry.get("yahoo").descriptor
    assert lookback_floor(d, "5m", NOW) == NOW - timedelta(days=60) + timedelta(minutes=10)


def test_an_interval_without_a_measured_limit_has_no_floor(registry):
    with pytest.raises(UnsupportedCapability):
        lookback_floor(registry.get("yahoo").descriptor, "15m", NOW)


def test_a_sixty_day_backfill_starts_at_the_floor_since_midnight_would_exceed_the_limit(registry):
    d = registry.get("yahoo").descriptor
    start, end = backfill_window(d, "5m", 60, NOW, CAL)
    assert start == lookback_floor(d, "5m", NOW) and end == NOW


def test_a_shorter_backfill_starts_at_a_local_midnight_to_avoid_the_truncated_first_bar(registry):
    start, end = backfill_window(registry.get("yahoo").descriptor, "5m", 30, NOW, CAL)
    local = start.astimezone(EXCHANGE_TZ)
    assert (local.hour, local.minute, local.second) == (0, 0, 0) and start <= NOW - timedelta(days=30)
    assert not CAL.session(local.date()) or start <= CAL.session(local.date()).open_ts  # never inside a session


def test_requests_for_more_than_the_provider_allows_are_clamped(registry):
    d = registry.get("yahoo").descriptor
    start, _ = backfill_window(d, "5m", 400, NOW, CAL)
    assert start == lookback_floor(d, "5m", NOW)


def test_clean_start_keeps_the_desired_time_when_midnight_would_breach_the_floor():
    floor = ist(5, 12, 0, month=9).astimezone(UTC)
    assert clean_start(floor, floor, CAL) == floor
    later = ist(20, 14, 30, month=10).astimezone(UTC)
    assert clean_start(later, floor, CAL) == ist(20, 0, 0, month=10).astimezone(UTC)


# ---- targets -------------------------------------------------------------------------------------------


def test_default_targets_are_the_ten_enabled_equities_never_futures(db, svc):
    targets = resolve_targets(svc, "yahoo", None)
    assert len(targets) == 10 and {t.symbol for t in targets}.isdisjoint({"NIFTY", "BANKNIFTY"})


def test_an_explicit_futures_id_is_an_error_not_silently_skipped(db, svc):
    nifty = db.scalar(select(Instrument.id).where(Instrument.symbol == "NIFTY"))
    with pytest.raises(InstrumentDisabled):
        resolve_targets(svc, "yahoo", [nifty])


def test_an_unknown_explicit_id_is_not_found(svc):
    with pytest.raises(InstrumentNotFound):
        resolve_targets(svc, "yahoo", [999999])


def test_duplicate_ids_are_collapsed(db, svc):
    rel = db.scalar(select(Instrument.id).where(Instrument.symbol == "RELIANCE"))
    assert len(resolve_targets(svc, "yahoo", [rel, rel, rel])) == 1


# ---- locking and recovery ----------------------------------------------------------------------------------


def test_only_one_job_can_be_active(db, svc, md_settings, clock):
    first = start_job(db, svc, md_settings, clock)
    with pytest.raises(JobAlreadyRunning) as info:
        start_job(db, svc, md_settings, clock)
    assert info.value.job_id == first.id


def test_a_finished_job_releases_the_lock(db, svc, md_settings, clock, sf, registry, provider):
    provider.bars = day_bars(WEEKDAYS[0])
    run(db, svc, sf, registry, md_settings, clock, ids=all_ids(svc)[:1])
    assert start_job(db, svc, md_settings, clock).status is JobStatus.QUEUED


def test_a_job_with_a_stale_heartbeat_is_marked_interrupted_and_replaced(db, svc, md_settings, clock):
    first = start_job(db, svc, md_settings, clock)
    clock.advance(seconds=md_settings.marketdata_job_heartbeat_timeout_seconds + 1)
    second = start_job(db, svc, md_settings, clock)
    db.expire_all()
    stale = db.get(DataIngestionJob, first.id)
    assert stale.status is JobStatus.INTERRUPTED and stale.finished_at is not None and "heartbeat" in stale.error_message
    assert second.status is JobStatus.QUEUED and second.id != first.id


def test_a_job_with_a_fresh_heartbeat_is_not_taken_over(db, svc, md_settings, clock):
    start_job(db, svc, md_settings, clock)
    clock.advance(seconds=md_settings.marketdata_job_heartbeat_timeout_seconds - 1)
    with pytest.raises(JobAlreadyRunning):
        start_job(db, svc, md_settings, clock)


def test_creating_a_job_for_an_unregistered_provider_is_refused(db, svc, md_settings, clock):
    with pytest.raises(UnsupportedCapability):
        create_job(db, job_type=JobType.BACKFILL, provider_code="nobody", timeframe="5m", parameters={}, user_id=None, settings=md_settings, clock=clock)


def test_running_a_job_that_is_not_queued_does_nothing(db, svc, md_settings, clock, sf, registry, provider):
    job = start_job(db, svc, md_settings, clock)
    job.status = JobStatus.SUCCEEDED
    db.commit()
    run_job(sf, registry, md_settings, job.id, clock=clock, sleep=NO_SLEEP)
    assert provider.calls == []


# ---- end to end ---------------------------------------------------------------------------------------------------


def test_a_backfill_archives_all_ten_equities_and_reports_per_instrument(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = days_bars(WEEKDAYS)
    job = run(db, svc, sf, registry, md_settings, clock)
    assert job.status is JobStatus.SUCCEEDED and job.started_at and job.finished_at and job.heartbeat_at >= job.started_at
    rows = job.summary["instruments"]
    assert len(rows) == 10 and job.summary["failed"] == 0
    assert all(r["status"] == "SUCCEEDED" and r["candles_inserted"] == 600 for r in rows)
    assert count(db, Candle) == 6000 and count(db, DataIngestionBatch) == 10
    assert rows[0]["window_end"] == iso_utc(NOW) and rows[0]["window_start"] == iso_utc(lookback_floor(registry.get("yahoo").descriptor, "5m", NOW))
    assert {c.provider_symbol for c in db.scalars(select(Candle))} == {f"{s}.NS" for s in ["RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY", "TCS", "LT", "BHARTIARTL", "ITC", "AXISBANK"]}


def test_rerunning_a_backfill_creates_no_duplicates(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = days_bars(WEEKDAYS[:2])
    ids = all_ids(svc)[:3]
    run(db, svc, sf, registry, md_settings, clock, ids=ids)
    second = run(db, svc, sf, registry, md_settings, clock, ids=ids)
    assert all(r["candles_inserted"] == 0 and r["duplicates_identical"] == 150 for r in second.summary["instruments"])
    assert count(db, Candle) == 3 * 150


def test_a_failing_instrument_makes_the_job_partial_and_the_others_still_succeed(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = day_bars(WEEKDAYS[0])
    provider.fail_symbols["TCS.NS"] = ProviderUnavailable("tcs outage")
    job = run(db, svc, sf, registry, md_settings, clock)
    assert job.status is JobStatus.PARTIAL and job.summary["failed"] == 1
    by = {r["symbol"]: r for r in job.summary["instruments"]}
    assert by["TCS"]["status"] == "FAILED" and by["TCS"]["error_kind"] == "UNAVAILABLE"
    assert all(r["status"] == "SUCCEEDED" for s, r in by.items() if s != "TCS")
    assert count(db, Candle) == 9 * 75


def test_when_every_instrument_fails_the_job_is_failed(db, svc, sf, registry, md_settings, clock, provider):
    provider.fail_with = ProviderUnavailable("down")
    job = run(db, svc, sf, registry, md_settings, clock, ids=all_ids(svc)[:3])
    assert job.status is JobStatus.FAILED and job.summary["failed"] == 3 and count(db, Candle) == 0


def test_an_unexpected_error_in_one_instrument_does_not_stop_the_rest(db, svc, sf, registry, md_settings, clock, provider, monkeypatch):
    provider.bars = day_bars(WEEKDAYS[0])
    ids = all_ids(svc)[:4]
    real = MarketDataService.ingest_window

    def flaky(self, **kw):
        if kw["instrument"].id == ids[1]:
            raise RuntimeError("unexpected")
        return real(self, **kw)

    monkeypatch.setattr(MarketDataService, "ingest_window", flaky)
    job = run(db, svc, sf, registry, md_settings, clock, ids=ids)
    statuses = [r["status"] for r in job.summary["instruments"]]
    assert statuses == ["SUCCEEDED", "FAILED", "SUCCEEDED", "SUCCEEDED"] and job.status is JobStatus.PARTIAL
    assert job.summary["instruments"][1]["error_kind"] == "INTERNAL_ERROR"


def test_a_crash_of_the_runner_itself_is_recorded_on_the_job(db, svc, sf, registry, md_settings, clock, monkeypatch):
    monkeypatch.setattr(jobs_module, "_run_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("runner exploded")))
    job = run(db, svc, sf, registry, md_settings, clock, ids=all_ids(svc)[:1])
    assert job.status is JobStatus.FAILED and "runner exploded" in job.error_message and job.finished_at


def test_a_job_with_a_disabled_instrument_in_its_parameters_skips_it_visibly(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = day_bars(WEEKDAYS[0])
    ids = all_ids(svc)[:2]
    nifty = db.scalar(select(Instrument.id).where(Instrument.symbol == "NIFTY"))
    job = run(db, svc, sf, registry, md_settings, clock, ids=ids + [nifty])  # bypasses the API's up-front validation
    skipped = [r for r in job.summary["instruments"] if r["status"] == "SKIPPED_DISABLED"]
    assert len(skipped) == 1 and skipped[0]["error_kind"] == "INSTRUMENT_DISABLED" and job.status is JobStatus.PARTIAL
    assert all(c[1] != "NIFTY" for c in provider.calls)


def test_the_runner_pauses_between_instruments_but_not_before_the_first(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = day_bars(WEEKDAYS[0])
    pauses = []
    settings = md_settings.model_copy(update={"marketdata_request_pause_seconds": 1.5})
    run(db, svc, sf, registry, md_settings, clock, ids=all_ids(svc)[:4], sleep=pauses.append, settings=settings)
    assert pauses == [1.5, 1.5, 1.5]


def test_futures_ingestion_never_reaches_the_provider(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = day_bars(WEEKDAYS[0])
    run(db, svc, sf, registry, md_settings, clock)
    assert provider.history_calls and all("NIFTY" not in c[1] for c in provider.history_calls)


# ---- incremental ------------------------------------------------------------------------------------------------------


def backfilled(db, svc, sf, registry, md_settings, clock, provider, ids):
    provider.bars = days_bars(WEEKDAYS)
    run(db, svc, sf, registry, md_settings, clock, ids=ids)


def test_incremental_adds_only_new_bars_and_overlaps_to_detect_changes(db, svc, sf, registry, md_settings, clock, provider):
    ids = all_ids(svc)[:2]
    backfilled(db, svc, sf, registry, md_settings, clock, provider, ids)
    clock.now = ist(12, 16, 0).astimezone(UTC)
    provider.bars += day_bars(date(2026, 11, 12))
    provider.calls.clear()
    job = run(db, svc, sf, registry, md_settings, clock, JobType.INCREMENTAL, ids)
    assert job.status is JobStatus.SUCCEEDED
    for r in job.summary["instruments"]:
        assert r["candles_inserted"] == 75 and r["duplicates_identical"] == 150 and r["restatements"] == 0
    # the overlap starts at the local midnight of (last archived bar end - 24h): intact first bar, no discard
    start = provider.history_calls[0][3]
    assert start == ist(10, 0, 0).astimezone(UTC)
    assert count(db, DataQualityIssue, DataQualityIssue.issue_type == IssueType.WINDOW_EDGE_TRUNCATED) == 0
    assert count(db, Candle) == 2 * 675


def test_incremental_flags_a_changed_overlapping_bar_as_a_restatement(db, svc, sf, registry, md_settings, clock, provider):
    ids = all_ids(svc)[:1]
    backfilled(db, svc, sf, registry, md_settings, clock, provider, ids)
    clock.now = ist(12, 16, 0).astimezone(UTC)
    idx = next(i for i, b in enumerate(provider.bars) if b.start_ts == ist(11, 10, 0))
    b = provider.bars[idx]
    provider.bars[idx] = RawBar(b.start_ts, b.open, b.high + 2, b.low, b.close + 2, b.volume)
    job = run(db, svc, sf, registry, md_settings, clock, JobType.INCREMENTAL, ids)
    assert job.summary["instruments"][0]["restatements"] == 1
    assert count(db, Candle, Candle.is_canonical.is_(False)) == 1


def test_incremental_with_an_empty_archive_falls_back_to_the_full_window(db, svc, sf, registry, md_settings, clock, provider):
    provider.bars = days_bars(WEEKDAYS)
    job = run(db, svc, sf, registry, md_settings, clock, JobType.INCREMENTAL, all_ids(svc)[:1])
    assert job.summary["instruments"][0]["candles_inserted"] == 600


def test_incremental_is_idempotent_when_nothing_is_new(db, svc, sf, registry, md_settings, clock, provider):
    ids = all_ids(svc)[:1]
    backfilled(db, svc, sf, registry, md_settings, clock, provider, ids)
    job = run(db, svc, sf, registry, md_settings, clock, JobType.INCREMENTAL, ids)
    r = job.summary["instruments"][0]
    assert r["candles_inserted"] == 0 and r["restatements"] == 0 and count(db, Candle) == 600


def test_an_archive_older_than_the_providers_lookback_is_reported_as_an_unrecoverable_gap(db, svc, sf, registry, md_settings, clock, provider):
    ids = all_ids(svc)[:1]
    old_day = date(2026, 8, 3)
    provider.bars = day_bars(old_day)
    clock.now = ist(4, 11, 0, month=8).astimezone(UTC)
    run(db, svc, sf, registry, md_settings, clock, ids=ids, days=2)  # archive an old day while it was within reach
    clock.now = NOW  # months later: the archive ends far outside the 60-day lookback
    provider.bars = days_bars(WEEKDAYS)
    job = run(db, svc, sf, registry, md_settings, clock, JobType.INCREMENTAL, ids)
    assert job.status is JobStatus.SUCCEEDED
    issue = db.scalar(select(DataQualityIssue).where(DataQualityIssue.issue_type == IssueType.UNRECOVERABLE_GAP))
    assert issue is not None and "cannot be recovered" in issue.message and issue.details["archive_ends"].startswith("2026-08-03")
    assert count(db, Candle, Candle.session_date == old_day) == 75  # the old archive is preserved, never deleted
    assert count(db, Candle, Candle.session_date == date(2026, 11, 11)) == 75


def test_old_archived_days_survive_a_later_backfill_that_no_longer_reaches_them(db, svc, sf, registry, md_settings, clock, provider):
    ids = all_ids(svc)[:1]
    provider.bars = day_bars(date(2026, 9, 14))
    clock.now = ist(15, 11, 0, month=9).astimezone(UTC)
    run(db, svc, sf, registry, md_settings, clock, ids=ids, days=3)
    clock.now = NOW
    provider.bars = days_bars(WEEKDAYS)
    run(db, svc, sf, registry, md_settings, clock, ids=ids)
    assert count(db, Candle, Candle.session_date == date(2026, 9, 14)) == 75 and count(db, Candle) == 75 + 600
