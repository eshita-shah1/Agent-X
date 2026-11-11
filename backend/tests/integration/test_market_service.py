"""MarketDataService against a real database and a deterministic fake provider."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.db.enums import (
    BatchStatus,
    CalendarDayKind,
    InstrumentType,
    JobStatus,
    JobType,
    Timeframe,
    TradingStatus,
    VerificationStatus,
)
from app.db.models import (
    AdjustedCandle,
    Candle,
    CorporateAction,
    DataIngestionBatch,
    DataIngestionJob,
    DataProvider,
    DataQualityIssue,
    DataRestatementEvent,
    ExchangeCalendarDay,
    Instrument,
    InstrumentProviderSymbol,
    ProviderCapability,
    ProviderHealthState,
)
from app.marketdata import archive
from app.marketdata.adjustment import compute_split_adjusted
from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.errors import InstrumentDisabled, NoDataReturned, ProviderUnavailable, RateLimited, UnsupportedCapability
from app.marketdata.pipeline import process_bars
from app.marketdata.probe_store import record_probe_report
from app.marketdata.service import InstrumentNotFound, MarketDataService
from app.marketdata.types import Capability, CapabilityStatus, DataStatus, IssueType, ProviderBarsResult, RawBar, RawCorporateAction
from tests.support import CAL, NOW, WEEKDAYS, day_bars, days_bars

NAN = float("nan")
START = datetime(2026, 9, 14, tzinfo=UTC)


def ist(day, h, m):
    return datetime(2026, 11, day, h, m, tzinfo=EXCHANGE_TZ)


@pytest.fixture
def job_id(db, svc):
    prov = db.scalar(select(DataProvider))
    job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.RUNNING, provider_id=prov.id, timeframe=Timeframe.M5)
    db.add(job)
    db.commit()
    return job.id


def inst(db, symbol):
    return db.scalar(select(Instrument).where(Instrument.symbol == symbol))


def ingest(svc, db, job_id, symbol="RELIANCE", start=START, end=NOW, **kw):
    return svc.ingest_window(job_id=job_id, instrument=inst(db, symbol), provider_code="yahoo", timeframe="5m", start=start, end=end, **kw)


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


# ---- successful ingestion ---------------------------------------------------------------------------


def test_a_window_is_archived_with_batch_provenance_and_provider_health(db, svc, provider, job_id):
    provider.bars = days_bars(WEEKDAYS)
    out = ingest(svc, db, job_id)
    assert out.status is BatchStatus.SUCCEEDED and out.candles_inserted == 600 and out.rows_received == 600
    batch = db.get(DataIngestionBatch, out.batch_id)
    assert (batch.status, batch.provider_symbol, batch.rows_received, batch.candles_inserted, batch.quarantined) == (BatchStatus.SUCCEEDED, "RELIANCE.NS", 600, 600, 0)
    assert batch.observed_at == NOW and batch.request_start == START and batch.request_end == NOW and batch.finished_at is not None
    assert batch.response_first_ts == ist(2, 9, 15) and batch.response_last_ts == ist(11, 15, 25)
    assert {c.ingestion_batch_id for c in db.scalars(select(Candle))} == {batch.id}
    health = db.get(ProviderHealthState, batch.provider_id)
    assert health.consecutive_failures == 0 and health.last_success_at == NOW and health.last_successful_ingestion_at == NOW
    assert health.last_observed_market_ts == ist(11, 15, 30)  # newest archived bar's end


def test_reingesting_the_same_window_is_idempotent(db, svc, provider, job_id):
    provider.bars = days_bars(WEEKDAYS[:3])
    ingest(svc, db, job_id)
    again = ingest(svc, db, job_id)
    assert (again.candles_inserted, again.duplicates_identical, again.restatements) == (0, 225, 0)
    assert count(db, Candle) == 225


def test_a_provider_restatement_is_recorded_without_touching_the_archive(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    ingest(svc, db, job_id)
    original = db.scalars(select(Candle).order_by(Candle.bar_start_ts)).first()
    snapshot = (original.open, original.high, original.low, original.close, original.volume)
    b = provider.bars[0]
    provider.bars[0] = RawBar(b.start_ts, b.open, b.high + 3, b.low, b.close + 3, b.volume + 99)
    out = ingest(svc, db, job_id)
    assert out.restatements == 1 and out.candles_inserted == 0
    db.refresh(original)
    assert (original.open, original.high, original.low, original.close, original.volume) == snapshot and original.is_canonical
    assert count(db, Candle) == 75 + 1 and count(db, DataRestatementEvent) == 1
    issue = db.scalar(select(DataQualityIssue).where(DataQualityIssue.issue_type == IssueType.RESTATEMENT))
    assert issue.details["changed_fields"].keys() == {"high", "close", "volume"}


def test_quality_issues_are_persisted_with_the_rejected_raw_row(db, svc, provider, job_id):
    bars = day_bars(WEEKDAYS[0], last=5)
    bars.append(RawBar(ist(2, 15, 20), NAN, NAN, NAN, NAN, 0.0))
    bars.append(RawBar(ist(2, 15, 25), 100, 90, 95, 99, 10))
    provider.bars = bars
    out = ingest(svc, db, job_id)
    assert out.quarantined == 2 and out.candles_inserted == 5
    issues = {i.issue_type: i for i in db.scalars(select(DataQualityIssue))}
    gap = issues[IssueType.NON_NUMERIC_VALUE]
    assert gap.details["provider_gap_marker"] is True and gap.raw_payload["volume"] == 0.0 and gap.bar_start_ts == ist(2, 15, 20)
    assert issues[IssueType.INVALID_OHLC].raw_payload["high"] == 90
    assert {i.batch_id for i in issues.values()} == {out.batch_id}


def test_a_bar_still_forming_is_not_archived(db, svc, provider, clock, job_id):
    provider.bars = day_bars(WEEKDAYS[-1], last=10)  # 09:15..10:00 starts
    clock.now = ist(11, 10, 2).astimezone(UTC)  # 10:02 IST: the 10:00 bar ends 10:05
    out = ingest(svc, db, job_id, start=ist(11, 0, 0).astimezone(UTC), end=clock.now)
    assert out.candles_inserted == 9 and out.non_final_dropped == 1
    assert count(db, Candle, Candle.bar_start_ts == ist(11, 10, 0).astimezone(UTC)) == 0
    assert count(db, DataQualityIssue, DataQualityIssue.issue_type == IssueType.INCOMPLETE_CANDLE) == 1


def test_it_waits_for_the_finalisation_margin_before_archiving_a_just_ended_bar(db, svc, provider, clock, job_id):
    provider.bars = day_bars(WEEKDAYS[-1], last=10)
    clock.now = ist(11, 10, 5).astimezone(UTC) + timedelta(seconds=30)  # ended 30 s ago; margin is 60 s
    assert ingest(svc, db, job_id, start=ist(11, 0, 0).astimezone(UTC), end=clock.now).candles_inserted == 9
    clock.now += timedelta(seconds=40)
    out = ingest(svc, db, job_id, start=ist(11, 0, 0).astimezone(UTC), end=clock.now)
    assert out.candles_inserted == 1 and out.duplicates_identical == 9


def test_a_request_starting_inside_a_session_discards_the_providers_truncated_first_bar(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[-1], last=30)
    out = ingest(svc, db, job_id, start=ist(11, 10, 7).astimezone(UTC))
    first_kept = db.scalar(select(func.min(Candle.bar_start_ts)))
    # the fake serves bars with start >= 10:07, so its first bar is 10:10 - within one step of the request start
    assert out.quarantined == 1 and first_kept == ist(11, 10, 15).astimezone(UTC)
    assert count(db, DataQualityIssue, DataQualityIssue.issue_type == IssueType.WINDOW_EDGE_TRUNCATED) == 1


def test_a_provider_not_known_to_truncate_keeps_its_first_bar(db, registry, md_settings, clock, seeded, job_id):
    from app.marketdata.providers.registry import ProviderRegistry
    from tests.support import FakeProvider

    honest = FakeProvider(day_bars(WEEKDAYS[-1], last=30), clock, truncates_first_bar=False)
    svc2 = MarketDataService(db, ProviderRegistry({"yahoo": honest}), md_settings, clock)
    out = ingest(svc2, db, job_id, start=ist(11, 10, 7).astimezone(UTC))
    assert out.quarantined == 0 and db.scalar(select(func.min(Candle.bar_start_ts))) == ist(11, 10, 10).astimezone(UTC)


def test_a_request_starting_at_or_before_the_open_keeps_every_bar(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[-1], last=30)
    out = ingest(svc, db, job_id, start=ist(11, 9, 15).astimezone(UTC))
    assert out.candles_inserted == 30 and out.quarantined == 0
    assert count(db, DataQualityIssue, DataQualityIssue.issue_type == IssueType.WINDOW_EDGE_TRUNCATED) == 0


def test_starts_inside_session_boundaries(svc):
    assert svc.starts_inside_session(ist(11, 10, 0).astimezone(UTC))
    assert not svc.starts_inside_session(ist(11, 9, 15).astimezone(UTC))  # exactly the open: clean
    assert not svc.starts_inside_session(ist(11, 8, 0).astimezone(UTC))
    assert not svc.starts_inside_session(ist(11, 15, 30).astimezone(UTC))
    assert not svc.starts_inside_session(ist(14, 10, 0).astimezone(UTC))  # Saturday


# ---- corporate actions ------------------------------------------------------------------------------


def test_corporate_actions_are_fetched_and_recorded_with_the_batch(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    provider.actions = [RawCorporateAction(date(2024, 10, 28), "SPLIT", 2.0), RawCorporateAction(date(2025, 6, 5), "DIVIDEND", 5.5)]
    out = ingest(svc, db, job_id)
    assert out.corporate_actions_recorded == 2 and count(db, CorporateAction) == 2
    assert {a.ingestion_batch_id for a in db.scalars(select(CorporateAction))} == {out.batch_id}
    assert ingest(svc, db, job_id).corporate_actions_recorded == 0  # idempotent


def test_corporate_action_fetch_failure_does_not_fail_the_candles(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    provider.actions_error = RateLimited("slow down")
    out = ingest(svc, db, job_id)
    assert out.status is BatchStatus.SUCCEEDED and out.candles_inserted == 75
    issue = db.scalar(select(DataQualityIssue).where(DataQualityIssue.issue_type == IssueType.CORPORATE_ACTIONS_UNAVAILABLE))
    assert issue is not None and issue.details["kind"] == "RATE_LIMITED"


def test_corporate_actions_can_be_skipped(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    ingest(svc, db, job_id, include_corporate_actions=False)
    assert [c for c in provider.calls if c[0] == "actions"] == []


# ---- failures: recorded, isolated, recoverable -----------------------------------------------------------


def test_a_provider_failure_is_recorded_on_the_batch_and_in_provider_health(db, svc, provider, job_id):
    provider.fail_with = ProviderUnavailable("connection reset")
    out = ingest(svc, db, job_id)
    assert out.status is BatchStatus.FAILED and out.error_kind == "UNAVAILABLE" and out.candles_inserted == 0
    batch = db.get(DataIngestionBatch, out.batch_id)
    assert batch.status is BatchStatus.FAILED and "connection reset" in batch.error_message and batch.finished_at
    h = svc.provider_health("yahoo")
    assert h.status is DataStatus.DEGRADED and h.consecutive_failures == 1 and h.last_error_kind == "UNAVAILABLE"
    assert count(db, Candle) == 0


def test_repeated_failures_make_the_provider_unavailable_and_success_recovers_it(db, svc, provider, job_id):
    provider.fail_with = RateLimited("429")
    for _ in range(3):
        out = ingest(svc, db, job_id)
    assert out.error_kind == "RATE_LIMITED" and svc.provider_health("yahoo").status is DataStatus.UNAVAILABLE
    provider.fail_with, provider.bars = None, day_bars(WEEKDAYS[0])
    ok = ingest(svc, db, job_id)  # failed-ingestion recovery: the same call simply works once the provider does
    assert ok.status is BatchStatus.SUCCEEDED and ok.candles_inserted == 75
    h = svc.provider_health("yahoo")
    assert h.status is DataStatus.HEALTHY and h.consecutive_failures == 0
    assert count(db, DataIngestionBatch, DataIngestionBatch.status == BatchStatus.FAILED) == 3  # history of the failures is kept


def test_no_data_is_recorded_as_no_data_and_is_not_a_provider_failure(db, svc, provider, job_id):
    provider.bars = []
    out = ingest(svc, db, job_id)
    assert out.status is BatchStatus.NO_DATA and out.error_kind == "NO_DATA"
    assert svc.provider_health("yahoo").status is DataStatus.HEALTHY
    assert db.get(DataIngestionBatch, out.batch_id).status is BatchStatus.NO_DATA


def test_an_internal_error_rolls_back_partial_writes_and_still_records_the_failure(db, svc, provider, job_id, monkeypatch):
    provider.bars = day_bars(WEEKDAYS[0])
    real = archive.archive_candles

    def boom(session, candles, **kw):
        real(session, candles, **kw)  # writes happen ...
        raise RuntimeError("disk full")  # ... then the batch blows up

    monkeypatch.setattr(archive, "archive_candles", boom)
    out = ingest(svc, db, job_id)
    assert out.status is BatchStatus.FAILED and out.error_kind == "INTERNAL_ERROR"
    assert count(db, Candle) == 0  # nothing half-written survives
    b = db.get(DataIngestionBatch, out.batch_id)
    assert b.status is BatchStatus.FAILED and "disk full" in b.error_message


def test_one_instruments_failure_does_not_affect_another(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    provider.fail_symbols["TCS.NS"] = ProviderUnavailable("tcs only")
    bad, good = ingest(svc, db, job_id, "TCS"), ingest(svc, db, job_id, "SBIN")
    assert bad.status is BatchStatus.FAILED and good.status is BatchStatus.SUCCEEDED and good.candles_inserted == 75


# ---- disabled instruments and unsupported requests ---------------------------------------------------------


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY"])
def test_futures_are_refused_before_any_provider_call_or_batch(db, svc, provider, job_id, symbol):
    with pytest.raises(InstrumentDisabled):
        ingest(svc, db, job_id, symbol)
    assert provider.calls == [] and count(db, DataIngestionBatch) == 0


def test_a_disabled_equity_is_refused(db, svc, provider, job_id):
    i = inst(db, "ITC")
    i.trading_status, i.disabled_reason = TradingStatus.DISABLED, "operator"
    db.commit()
    with pytest.raises(InstrumentDisabled):
        ingest(svc, db, job_id, "ITC")
    assert provider.calls == []


def test_an_instrument_without_an_explicit_symbol_mapping_is_refused_not_guessed(db, svc, provider, job_id):
    db.execute(delete(InstrumentProviderSymbol).where(InstrumentProviderSymbol.instrument_id == inst(db, "LT").id))
    db.commit()
    with pytest.raises(InstrumentDisabled, match="no explicit symbol mapping"):
        ingest(svc, db, job_id, "LT")
    assert provider.calls == []


def test_an_interval_without_a_measured_lookback_is_refused(db, svc, provider, job_id):
    with pytest.raises(UnsupportedCapability, match="no verified lookback"):
        svc.ingest_window(job_id=job_id, instrument=inst(db, "ITC"), provider_code="yahoo", timeframe="15m", start=START, end=NOW)
    assert provider.calls == []


def test_an_unregistered_provider_is_refused_without_a_silent_fallback(db, svc, provider, job_id):
    with pytest.raises(UnsupportedCapability):
        svc.ingest_window(job_id=job_id, instrument=inst(db, "ITC"), provider_code="somebody_else", timeframe="5m", start=START, end=NOW)
    assert provider.calls == []


def test_a_provider_configured_in_the_database_but_not_the_registry_is_refused(db, svc, registry, job_id):
    db.add(DataProvider(code="ghost", name="Ghost"))
    db.commit()
    with pytest.raises(UnsupportedCapability):
        svc.ingest_window(job_id=job_id, instrument=inst(db, "ITC"), provider_code="ghost", timeframe="5m", start=START, end=NOW)


def test_unknown_instrument_ids_raise_not_found(svc):
    with pytest.raises(InstrumentNotFound):
        svc.get_instrument(424242)


# ---- reading archived candles ------------------------------------------------------------------------------


def archive_all(svc, db, provider, job_id, days=WEEKDAYS, symbol="RELIANCE"):
    provider.bars = days_bars(days)
    return ingest(svc, db, job_id, symbol)


def test_candles_come_back_ordered_complete_and_with_provenance(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:2])
    rows, nxt = svc.get_candles(instrument_id=inst(db, "RELIANCE").id, timeframe="5m", start=START, end=NOW, limit=1000)
    assert len(rows) == 150 and nxt is None
    assert [r["bar_start_ts"] for r in rows] == sorted(r["bar_start_ts"] for r in rows)
    r = rows[0]
    assert (r["provider"], r["provider_symbol"], r["price_basis"], r["series"], r["is_canonical"], r["observation_version"]) == ("yahoo", "RELIANCE.NS", "UNVERIFIED_PROVIDER_NATIVE", "raw", True, 1)
    assert isinstance(r["open"], Decimal) and r["observed_at"] == NOW and r["ingested_at"] is not None


def test_pagination_covers_every_candle_exactly_once(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:3])
    iid, seen, cursor, pages = inst(db, "RELIANCE").id, [], None, 0
    while True:
        rows, cursor = svc.get_candles(instrument_id=iid, timeframe="5m", start=START, end=NOW, limit=100, after=cursor)
        seen += [r["candle_id"] for r in rows]
        pages += 1
        if cursor is None:
            break
    assert pages == 3 and len(seen) == 225 and len(set(seen)) == 225


def test_the_time_range_is_half_open(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:1])
    a, b = ist(2, 9, 15).astimezone(UTC), ist(2, 9, 25).astimezone(UTC)
    rows, _ = svc.get_candles(instrument_id=inst(db, "RELIANCE").id, timeframe="5m", start=a, end=b, limit=100)
    assert [r["bar_start_ts"] for r in rows] == [a, ist(2, 9, 20).astimezone(UTC)]


def test_restated_versions_are_not_returned_as_the_canonical_series(db, svc, provider, job_id):
    provider.bars = day_bars(WEEKDAYS[0])
    ingest(svc, db, job_id)
    b = provider.bars[3]
    provider.bars[3] = RawBar(b.start_ts, b.open, b.high + 5, b.low, b.close + 5, b.volume)
    ingest(svc, db, job_id)
    rows, _ = svc.get_candles(instrument_id=inst(db, "RELIANCE").id, timeframe="5m", start=START, end=NOW, limit=1000)
    assert len(rows) == 75 and all(r["is_canonical"] and r["observation_version"] == 1 for r in rows)


def test_disabled_instruments_have_no_candles_to_read(db, svc):
    with pytest.raises(InstrumentDisabled):
        svc.get_candles(instrument_id=inst(db, "NIFTY").id, timeframe="5m", start=START, end=NOW, limit=10)


def test_the_adjusted_series_is_separate_and_only_contains_computed_rows(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:1])
    iid = inst(db, "RELIANCE").id
    none, _ = svc.get_candles(instrument_id=iid, timeframe="5m", start=START, end=NOW, limit=10, series="adjusted")
    assert none == []  # nothing computed yet: no silent fallback to raw
    compute_split_adjusted(db, instrument_id=iid, provider_id=db.scalar(select(DataProvider.id)), timeframe=Timeframe.M5)
    db.commit()
    adj, _ = svc.get_candles(instrument_id=iid, timeframe="5m", start=START, end=NOW, limit=10, series="adjusted")
    raw, _ = svc.get_candles(instrument_id=iid, timeframe="5m", start=START, end=NOW, limit=10, series="raw")
    assert len(adj) == 10 and adj[0]["series"] == "adjusted" and adj[0]["adjustment_method"] == "SPLIT" and adj[0]["corporate_action_set_version"]
    assert raw[0]["series"] == "raw" and "adjustment_factor" not in raw[0]


# ---- coverage ------------------------------------------------------------------------------------------------


def coverage(svc, db, symbol="RELIANCE", start=date(2026, 11, 2), end=date(2026, 11, 11)):
    return svc.coverage(instrument_id=inst(db, symbol).id, timeframe="5m", start_day=start, end_day=end)


def test_complete_data_earns_full_coverage(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id)
    c = coverage(svc, db)
    assert c["candle_count"] == 600 and c["expected_candles"] == 600 and c["missing_intervals"] == 0
    assert c["days_complete"] == 8 and c["days_partial"] == 0 and c["days_absent"] == 0 and c["full_coverage_of_requested_range"] is True
    assert c["first_candle_start"] == ist(2, 9, 15) and c["last_candle_end"] == ist(11, 15, 30)
    assert c["calendar"]["explicit_overrides_loaded"] == 0 and "O13" in c["notes"][0]


def test_provider_nan_gaps_show_up_as_missing_intervals_and_block_full_coverage(db, svc, provider, job_id):
    bars = days_bars(WEEKDAYS)
    for i, b in enumerate(bars):
        if b.start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") in ("15:20", "15:25"):
            bars[i] = RawBar(b.start_ts, NAN, NAN, NAN, NAN, 0.0)
    provider.bars = bars
    ingest(svc, db, job_id)
    c = coverage(svc, db)
    assert c["missing_intervals"] == 16 and c["days_partial"] == 8 and c["full_coverage_of_requested_range"] is False
    assert c["partial_days"][0]["missing"] == 2 and c["quality_issue_counts"]["NON_NUMERIC_VALUE"] == 16


def test_an_absent_weekday_is_an_unexplained_absence_for_a_single_instrument(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, [d for d in WEEKDAYS if d != date(2026, 11, 10)])
    c = coverage(svc, db)
    assert c["absent_days"] == [date(2026, 11, 10)] and c["unexplained_absences"] == [date(2026, 11, 10)]
    assert c["probable_market_closures_inferred"] == [] and c["full_coverage_of_requested_range"] is False


def test_an_absence_shared_by_every_instrument_is_listed_as_an_inferred_closure(db, svc, provider, job_id):
    days = [d for d in WEEKDAYS if d != date(2026, 11, 10)]
    provider.bars = days_bars(days)
    for sym in ("RELIANCE", "TCS", "SBIN"):
        ingest(svc, db, job_id, sym)
    c = coverage(svc, db)
    assert c["probable_market_closures_inferred"] == [date(2026, 11, 10)] and c["unexplained_absences"] == []
    assert c["full_coverage_of_requested_range"] is True  # earned: the only absence is corroborated across instruments
    assert any("inferred" in n for n in c["notes"])


def test_an_explicit_calendar_holiday_is_not_expected(db, registry, md_settings, clock, provider, job_id, seeded):
    db.add(ExchangeCalendarDay(exchange="NSE", day=date(2026, 11, 10), kind=CalendarDayKind.HOLIDAY, source="test-calendar"))
    db.commit()
    svc2 = MarketDataService(db, registry, md_settings, clock)
    provider.bars = days_bars([d for d in WEEKDAYS if d != date(2026, 11, 10)])
    ingest(svc2, db, job_id)
    c = coverage(svc2, db)
    assert c["days_absent"] == 0 and c["missing_intervals"] == 0 and c["calendar"]["explicit_overrides_loaded"] == 1
    assert c["full_coverage_of_requested_range"] is True


def test_coverage_before_the_first_archived_day_is_not_full(db, svc, provider, job_id):
    archive_all(svc, db, provider, job_id, WEEKDAYS[3:])
    c = coverage(svc, db)  # asks from 2 Nov, archive begins 5 Nov
    assert c["full_coverage_of_requested_range"] is False and c["days_absent"] == 3


def test_coverage_of_an_empty_archive_reports_nothing_and_claims_nothing(db, svc):
    c = coverage(svc, db)
    assert c["candle_count"] == 0 and c["first_candle_start"] is None and c["full_coverage_of_requested_range"] is False


def test_future_slots_of_today_are_not_counted_as_missing(db, svc, provider, clock, job_id):
    provider.bars = day_bars(WEEKDAYS[-1], last=47)  # 09:15..13:05
    clock.now = ist(11, 13, 12).astimezone(UTC)
    ingest(svc, db, job_id, start=ist(11, 0, 0).astimezone(UTC), end=clock.now)
    c = coverage(svc, db, start=date(2026, 11, 11))
    assert c["expected_candles"] == 47 and c["missing_intervals"] == 0


# ---- freshness and health ----------------------------------------------------------------------------------------


def test_freshness_is_unavailable_without_data_and_healthy_after_the_close(db, svc, provider, job_id):
    iid = inst(db, "RELIANCE").id
    assert svc.freshness(instrument_id=iid, timeframe="5m")["status"] == "UNAVAILABLE"
    archive_all(svc, db, provider, job_id)
    f = svc.freshness(instrument_id=iid, timeframe="5m")
    assert f["status"] == "HEALTHY" and f["latest_finalized_bar_end"] == ist(11, 15, 30) and f["lag_seconds"] == 0


def test_freshness_turns_stale_when_the_archive_stops_while_the_market_moves_on(db, svc, provider, clock, job_id):
    archive_all(svc, db, provider, job_id)
    clock.now = ist(12, 11, 0).astimezone(UTC)  # next trading morning, nothing newer archived
    f = svc.freshness(instrument_id=inst(db, "RELIANCE").id, timeframe="5m")
    assert f["status"] == "STALE" and f["lag_bars"] > 3 and f["data_age_seconds"] > 0


def test_a_never_used_provider_is_unknown_not_healthy(svc):
    assert svc.provider_health("yahoo").status is DataStatus.UNKNOWN


def test_thresholds_are_labelled_proposed_and_unapproved(svc):
    t = svc.thresholds_report()
    assert set(t) == {"finalization_margin_seconds", "stale_after_minutes", "provider_unavailable_after_consecutive_failures"}
    assert all(v["status"] == "PROVISIONAL" and "Do not use" in v["note"] for v in t.values())
    assert t["finalization_margin_seconds"]["value"] == 60


# ---- capabilities and probe persistence -----------------------------------------------------------------------------


def probe_report(ok=True, kinds=None):
    five = {"success": True, "returned_candles": 2897, "first_bar_start_ist": "2026-09-15", "last_bar_lag_minutes": -2.0, "intraday_adj_close_differs_from_close": 0, "extras_present": ["adj_close"]}
    sym = lambda name, good: {  # noqa: E731
        "symbol": name, "provider_symbol": f"{name}.NS", "accepted_lookback_days": 60,
        "five_minute": five if good else {"success": False}, "five_minute_attempts": [{"error_kind": k} for k in (kinds or [])] or [{"error_kind": None}],
        "daily": {"success": True}, "corporate_actions": {"success": True}, "quote": {"success": True}, "metadata": {"success": True},
        "request_meta": {"index_tz": "Asia/Kolkata"}, "price_basis": {"classification": "X", "split_tests": []},
    }
    return {"probe_id": "probe-1", "provider": "yahoo", "target_days": 60, "symbols": [sym("RELIANCE", True), sym("TCS", ok)]}


def test_capabilities_default_to_unknown_for_anything_never_established(db, svc):
    caps = {c["capability"]: c for c in svc.capabilities("yahoo")}
    assert set(caps) == {c.value for c in Capability}
    assert caps["FUTURES_CONTRACT_DATA"]["status"] == "UNKNOWN" and caps["QUOTE_RETRIEVAL"]["status"] == "UNKNOWN"
    assert caps["FIVE_MINUTE_INTERVAL"]["status"] == "DOCUMENTED"
    assert not any(c["status"] == "VERIFIED" for c in caps.values())


def test_a_probe_report_upgrades_capabilities_and_verifies_symbols(db, svc):
    out = record_probe_report(db, probe_report())
    assert out["symbols_verified"] == 2
    caps = {c["capability"]: c for c in svc.capabilities("yahoo")}
    assert caps["FIVE_MINUTE_INTERVAL"]["status"] == "VERIFIED" and caps["FIVE_MINUTE_INTERVAL"]["probe_id"] == "probe-1"
    assert caps["QUOTE_RETRIEVAL"]["status"] == "VERIFIED" and caps["ADJUSTED_INTRADAY_PRICES"]["status"] == "UNSUPPORTED"
    assert caps["FUTURES_CONTRACT_DATA"]["status"] == "UNKNOWN"  # never probed, never upgraded
    m = db.scalar(select(InstrumentProviderSymbol).join(Instrument).where(Instrument.symbol == "RELIANCE"))
    assert m.verification_status is VerificationStatus.VERIFIED and m.verified_at and m.verification_evidence["probe_id"] == "probe-1"


def test_a_definitive_failure_marks_a_symbol_failed_but_a_network_blip_changes_nothing(db, svc):
    record_probe_report(db, probe_report(ok=False, kinds=["NO_DATA"]))
    status = lambda s: db.scalar(select(InstrumentProviderSymbol.verification_status).join(Instrument).where(Instrument.symbol == s))  # noqa: E731
    assert status("TCS") is VerificationStatus.FAILED and status("RELIANCE") is VerificationStatus.VERIFIED
    record_probe_report(db, probe_report(ok=False, kinds=["RATE_LIMITED"]))
    assert status("TCS") is VerificationStatus.FAILED  # unchanged by the transient failure; not re-marked either


def test_recording_a_probe_for_an_unregistered_provider_is_refused(db, svc):
    with pytest.raises(ValueError, match="seed"):
        record_probe_report(db, {**probe_report(), "provider": "nobody"})


def test_candles_stay_unverified_whatever_the_probe_says(db, svc, provider, job_id):
    record_probe_report(db, probe_report())
    archive_all(svc, db, provider, job_id, WEEKDAYS[:1])
    assert {c.price_basis.value for c in db.scalars(select(Candle))} == {"UNVERIFIED_PROVIDER_NATIVE"}
    assert all(c.basis_verified_by is None for c in db.scalars(select(Candle)))


# ---- reads serve only the configured active provider ------------------------------------------------


@pytest.fixture
def other(db, job_id):
    """A second registered provider holding its own archive (test data only: nothing ingests from it).
    Returns a function that archives `days` of bars for a symbol under that provider."""
    row = DataProvider(code="other", name="Other (test only)")
    db.add(row)
    db.commit()

    def archive_other(days, symbol="RELIANCE", price=500.0):
        i = inst(db, symbol)
        batch = DataIngestionBatch(
            job_id=job_id, instrument_id=i.id, provider_id=row.id, timeframe=Timeframe.M5, provider_symbol=f"{symbol}.OT",
            request_start=START, request_end=NOW, status=BatchStatus.SUCCEEDED,
        )
        db.add(batch)
        db.commit()
        res = ProviderBarsResult("other", f"{symbol}.OT", "5m", START, NOW, days_bars(days, price=price), NOW)
        candles = process_bars(res, instrument_id=i.id, timeframe="5m", calendar=CAL, finalization_margin=timedelta(minutes=1)).candles
        out = archive.archive_candles(db, candles, provider_id=row.id, batch_id=batch.id)
        db.commit()
        return out

    return row, archive_other


def as_provider(db, registry, md_settings, clock, code):
    return MarketDataService(db, registry, md_settings.model_copy(update={"marketdata_provider": code}), clock)


def test_candles_never_mix_providers_and_pagination_walks_only_the_active_one(db, svc, provider, job_id, other):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:2])  # yahoo: 150 bars
    other[1](WEEKDAYS[:3])  # other: 225 bars, same instrument and slots, different prices
    rid = inst(db, "RELIANCE").id
    window = dict(instrument_id=rid, timeframe="5m", start=ist(2, 0, 0), end=ist(12, 0, 0))
    rows, nxt = svc.get_candles(**window, limit=1000)
    assert len(rows) == 150 and nxt is None and {r["provider"] for r in rows} == {"yahoo"}
    assert all(r["close"] < 200 for r in rows)  # yahoo's ~100 prices, never the other provider's ~500
    seen, cursor = [], None
    while True:
        page, cursor = svc.get_candles(**window, limit=40, after=cursor)
        seen += [r["candle_id"] for r in page]
        if cursor is None:
            break
    assert len(seen) == 150 == len(set(seen)) and set(seen) == {r["candle_id"] for r in rows}


def test_the_configured_provider_decides_which_series_is_read(db, registry, md_settings, clock, svc, provider, job_id, other):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:2])
    other[1](WEEKDAYS[:3])
    alt = as_provider(db, registry, md_settings, clock, "other")
    rows, _ = alt.get_candles(instrument_id=inst(db, "RELIANCE").id, timeframe="5m", start=ist(2, 0, 0), end=ist(12, 0, 0), limit=1000)
    assert len(rows) == 225 and {r["provider"] for r in rows} == {"other"} and all(r["close"] > 400 for r in rows)


def test_an_unregistered_active_provider_is_refused_not_mixed(db, registry, md_settings, clock, seeded):
    with pytest.raises(UnsupportedCapability):
        as_provider(db, registry, md_settings, clock, "nobody").get_candles(
            instrument_id=inst(db, "RELIANCE").id, timeframe="5m", start=ist(2, 0, 0), end=ist(12, 0, 0), limit=10
        )


def test_coverage_and_freshness_ignore_other_providers(db, svc, provider, job_id, other):
    archive_all(svc, db, provider, job_id, WEEKDAYS[:-1])  # yahoo stops at 11-10
    other[1](WEEKDAYS)  # other reaches 11-11
    other[1](WEEKDAYS[:1], price=600.0)  # and restates 11-02 under its own provider
    c = coverage(svc, db)
    assert c["candle_count"] == 525 and c["last_candle_end"] == ist(10, 15, 30).astimezone(UTC) and c["restated_bars"] == 0
    assert c["absent_days"] == [date(2026, 11, 11)]
    f = svc.freshness(instrument_id=inst(db, "RELIANCE").id, timeframe="5m")
    assert f["latest_finalized_bar_end"] == ist(10, 15, 30).astimezone(UTC) and f["status"] == "STALE"


def test_another_providers_data_cannot_hide_an_inferred_closure(db, svc, provider, job_id, other):
    days = [d for d in WEEKDAYS if d != date(2026, 11, 10)]
    provider.bars = days_bars(days)
    for sym in ("RELIANCE", "TCS"):
        ingest(svc, db, job_id, sym)
    other[1]([date(2026, 11, 10)])  # the other provider has RELIANCE bars on the day yahoo has none
    c = coverage(svc, db)
    assert c["probable_market_closures_inferred"] == [date(2026, 11, 10)] and c["unexplained_absences"] == []
