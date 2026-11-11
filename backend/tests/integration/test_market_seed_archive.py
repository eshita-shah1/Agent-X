"""Seeding, archiving (idempotency, restatements), corporate actions and the raw/adjusted separation."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import (
    AdjustmentMethod,
    BatchStatus,
    CorporateActionType,
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
    DataRestatementEvent,
    Instrument,
    InstrumentProviderSymbol,
    ProviderCapability,
)
from app.marketdata import archive
from app.marketdata.adjustment import compute_split_adjusted
from app.marketdata.pipeline import process_bars
from app.marketdata.registry import APPROVED_EQUITIES
from app.marketdata.seed import seed_registry
from app.marketdata.types import Capability, CapabilityStatus, IssueType, NormalizedCandle, ProviderBarsResult, RawCorporateAction
from datetime import timedelta as _td
from tests.support import CAL, NOW, day_bars

D = date(2026, 11, 10)


# ---- seeding --------------------------------------------------------------------------------------


def test_seed_registers_ten_enabled_equities_and_two_disabled_futures(db, registry, seeded):
    assert seeded == {"providers_added": 1, "instruments_added": 12, "symbol_mappings_added": 10, "capabilities_added": 4}
    eq = db.scalars(select(Instrument).where(Instrument.instrument_type == InstrumentType.EQUITY).order_by(Instrument.id)).all()
    assert [i.symbol for i in eq] == [e.symbol for e in APPROVED_EQUITIES]
    assert all(i.trading_status is TradingStatus.ENABLED and i.currency == "INR" and i.timezone == "Asia/Kolkata" and i.exchange == "NSE" for i in eq)
    fut = db.scalars(select(Instrument).where(Instrument.instrument_type == InstrumentType.FUTURE).order_by(Instrument.symbol)).all()
    assert [i.symbol for i in fut] == ["BANKNIFTY", "NIFTY"]
    assert all(i.trading_status is TradingStatus.DISABLED and i.disabled_reason and i.expiry_date is None and i.contract_identifier is None for i in fut)


def test_seed_maps_explicit_provider_symbols_unverified_and_none_for_futures(db, seeded):
    rows = db.execute(
        select(Instrument.symbol, Instrument.instrument_type, InstrumentProviderSymbol.provider_symbol, InstrumentProviderSymbol.verification_status)
        .join(InstrumentProviderSymbol, InstrumentProviderSymbol.instrument_id == Instrument.id)
    ).all()
    assert len(rows) == 10 and all(r[1] is InstrumentType.EQUITY for r in rows)
    assert all(r[2] == f"{r[0]}.NS" and r[3] is VerificationStatus.UNVERIFIED for r in rows)


def test_seed_is_idempotent(db, registry, seeded):
    again = seed_registry(db, registry.descriptors())
    assert again == {"providers_added": 0, "instruments_added": 0, "symbol_mappings_added": 0, "capabilities_added": 0}
    assert db.scalar(select(func.count()).select_from(Instrument)) == 12


def test_seed_never_re_enables_a_disabled_instrument(db, registry, seeded):
    inst = db.scalar(select(Instrument).where(Instrument.symbol == "SBIN"))
    inst.trading_status, inst.disabled_reason = TradingStatus.DISABLED, "operator decision"
    db.commit()
    seed_registry(db, registry.descriptors())
    db.refresh(inst)
    assert inst.trading_status is TradingStatus.DISABLED and inst.disabled_reason == "operator decision"


def test_seed_never_overwrites_probe_evidence(db, registry, seeded):
    row = db.scalar(select(ProviderCapability).where(ProviderCapability.capability == Capability.QUOTE_RETRIEVAL))
    row.status, row.evidence = CapabilityStatus.VERIFIED, {"probe_id": "p1"}
    db.commit()
    seed_registry(db, registry.descriptors())
    db.refresh(row)
    assert row.status is CapabilityStatus.VERIFIED and row.evidence == {"probe_id": "p1"}


# ---- archive helpers ---------------------------------------------------------------------------------


@pytest.fixture
def ctx(db, svc):
    inst = db.scalar(select(Instrument).where(Instrument.symbol == "RELIANCE"))
    prov = db.scalar(select(DataProvider).where(DataProvider.code == "yahoo"))
    job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=prov.id, timeframe=Timeframe.M5)
    db.add(job)
    db.commit()

    def new_batch():
        b = DataIngestionBatch(job_id=job.id, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5, provider_symbol="RELIANCE.NS", request_start=NOW - _td(days=1), request_end=NOW, status=BatchStatus.SUCCEEDED)
        db.add(b)
        db.commit()
        return b.id

    return inst, prov, new_batch


def candles_for(inst, bars, observed=NOW):
    res = ProviderBarsResult("yahoo", "RELIANCE.NS", "5m", observed - timedelta(days=1), observed, bars, observed)
    return process_bars(res, instrument_id=inst.id, timeframe="5m", calendar=CAL, finalization_margin=timedelta(minutes=5)).candles


def test_archiving_inserts_new_candles_as_canonical_version_one(db, ctx):
    inst, prov, nb = ctx
    r = archive.archive_candles(db, candles_for(inst, day_bars(D)), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert (r.inserted, r.duplicates_identical, r.restatements) == (75, 0, 0)
    rows = db.scalars(select(Candle)).all()
    assert len(rows) == 75 and all(c.is_canonical and c.observation_version == 1 and c.is_final for c in rows)
    assert all(c.price_basis.value == "UNVERIFIED_PROVIDER_NATIVE" and c.provider_symbol == "RELIANCE.NS" for c in rows)


def test_reingesting_unchanged_candles_is_a_no_op(db, ctx):
    inst, prov, nb = ctx
    cs = candles_for(inst, day_bars(D))
    archive.archive_candles(db, cs, provider_id=prov.id, batch_id=nb())
    db.commit()
    again = archive.archive_candles(db, candles_for(inst, day_bars(D)), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert (again.inserted, again.duplicates_identical, again.restatements) == (0, 75, 0)
    assert db.scalar(select(func.count()).select_from(Candle)) == 75


def test_an_overlapping_window_inserts_only_the_new_bars(db, ctx):
    inst, prov, nb = ctx
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=40)), provider_id=prov.id, batch_id=nb())
    db.commit()
    r = archive.archive_candles(db, candles_for(inst, day_bars(D, first=30)), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert r.inserted == 35 and r.duplicates_identical == 10 and r.restatements == 0
    assert db.scalar(select(func.count()).select_from(Candle)) == 75


def test_changed_values_are_recorded_as_a_restatement_and_the_original_is_kept(db, ctx):
    inst, prov, nb = ctx
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=3)), provider_id=prov.id, batch_id=nb())
    db.commit()
    restated = day_bars(D, last=3)
    b = restated[1]
    restated[1] = b.__class__(b.start_ts, b.open, b.high + 1, b.low, b.close + 1, b.volume + 5)  # high moves too: stays a valid OHLC
    r = archive.archive_candles(db, candles_for(inst, restated), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert (r.inserted, r.duplicates_identical, r.restatements) == (0, 2, 1)
    versions = db.scalars(select(Candle).where(Candle.bar_start_ts == restated[1].start_ts.astimezone(UTC)).order_by(Candle.observation_version)).all()
    assert [(v.observation_version, v.is_canonical) for v in versions] == [(1, True), (2, False)]
    original, newer = versions
    assert newer.close == original.close + 1 and original.close == Decimal("100.300000")  # original untouched
    event = db.scalar(select(DataRestatementEvent))
    assert event.original_candle_id == original.id and event.restated_candle_id == newer.id
    assert set(event.changed_fields) == {"high", "close", "volume"} and event.changed_fields["close"][0] == "100.300000"
    assert [i.issue_type for i in r.issues] == [IssueType.RESTATEMENT]


def test_the_same_restatement_seen_again_is_idempotent(db, ctx):
    inst, prov, nb = ctx
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=2)), provider_id=prov.id, batch_id=nb())
    db.commit()
    changed = [b.__class__(b.start_ts, b.open + 2, b.high + 2, b.low + 2, b.close + 2, b.volume) for b in day_bars(D, last=2)]
    first = archive.archive_candles(db, candles_for(inst, changed), provider_id=prov.id, batch_id=nb())
    db.commit()
    second = archive.archive_candles(db, candles_for(inst, changed), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert first.restatements == 2 and second.restatements == 0 and second.duplicates_identical == 2
    assert db.scalar(select(func.count()).select_from(Candle)) == 4
    assert db.scalar(select(func.count()).select_from(DataRestatementEvent)) == 2


def test_a_third_distinct_value_becomes_version_three(db, ctx):
    inst, prov, nb = ctx
    for bump in (0, 1, 2):
        bars = [b.__class__(b.start_ts, b.open, b.high + bump, b.low, b.close, b.volume) for b in day_bars(D, last=1)]
        archive.archive_candles(db, candles_for(inst, bars), provider_id=prov.id, batch_id=nb())
        db.commit()
    assert [(c.observation_version, c.is_canonical) for c in db.scalars(select(Candle).order_by(Candle.observation_version))] == [(1, True), (2, False), (3, False)]


def test_a_restatement_back_to_an_earlier_value_creates_no_new_row(db, ctx):
    inst, prov, nb = ctx
    original = day_bars(D, last=1)
    changed = [b.__class__(b.start_ts, b.open, b.high + 1, b.low, b.close, b.volume) for b in original]
    for bars in (original, changed, original):
        archive.archive_candles(db, candles_for(inst, bars), provider_id=prov.id, batch_id=nb())
        db.commit()
    assert db.scalar(select(func.count()).select_from(Candle)) == 2  # no flip-flopping into ever more versions


def test_raw_candles_cannot_be_modified_by_the_archive_path(db, ctx):
    inst, prov, nb = ctx
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=2)), provider_id=prov.id, batch_id=nb())
    db.commit()
    before = [(c.id, c.open, c.high, c.low, c.close, c.volume, c.observed_at) for c in db.scalars(select(Candle).order_by(Candle.id))]
    bumped = [b.__class__(b.start_ts, b.open + 5, b.high + 5, b.low + 5, b.close + 5, b.volume) for b in day_bars(D, last=2)]
    archive.archive_candles(db, candles_for(inst, bumped, observed=NOW + timedelta(hours=1)), provider_id=prov.id, batch_id=nb())
    db.commit()
    after = [(c.id, c.open, c.high, c.low, c.close, c.volume, c.observed_at) for c in db.scalars(select(Candle).where(Candle.observation_version == 1).order_by(Candle.id))]
    assert after == before


def test_archived_history_survives_a_later_window_that_excludes_it(db, ctx):
    inst, prov, nb = ctx
    old_day, new_day = date(2026, 11, 3), date(2026, 11, 10)
    archive.archive_candles(db, candles_for(inst, day_bars(old_day)), provider_id=prov.id, batch_id=nb())
    db.commit()
    archive.archive_candles(db, candles_for(inst, day_bars(new_day)), provider_id=prov.id, batch_id=nb())
    db.commit()
    days = {d for (d,) in db.execute(select(Candle.session_date).distinct())}
    assert days == {old_day, new_day}


def test_latest_canonical_bar_end(db, ctx):
    inst, prov, nb = ctx
    assert archive.latest_canonical_bar_end(db, inst.id, prov.id, Timeframe.M5) is None
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=10)), provider_id=prov.id, batch_id=nb())
    db.commit()
    assert archive.latest_canonical_bar_end(db, inst.id, prov.id, Timeframe.M5) == CAL.expected_bar_starts(CAL.session(D), "5m")[9] + timedelta(minutes=5)


def test_an_empty_candle_list_is_harmless(db, ctx):
    inst, prov, nb = ctx
    r = archive.archive_candles(db, [], provider_id=prov.id, batch_id=nb())
    assert (r.inserted, r.duplicates_identical, r.restatements) == (0, 0, 0)


# ---- corporate actions ---------------------------------------------------------------------------------


def actions(*a):
    return list(a)


def test_corporate_actions_are_recorded_with_provenance(db, ctx):
    inst, prov, nb = ctx
    n, issues = archive.record_corporate_actions(
        db, instrument_id=inst.id, provider_id=prov.id, source="yahoo", batch_id=nb(), observed_at=NOW,
        actions=[RawCorporateAction(date(2024, 10, 28), "SPLIT", 2.0), RawCorporateAction(date(2025, 6, 5), "DIVIDEND", 5.5)],
    )
    db.commit()
    assert n == 2 and not issues
    split = db.scalar(select(CorporateAction).where(CorporateAction.action_type == CorporateActionType.SPLIT))
    assert (split.ratio_numerator, split.ratio_denominator, split.cash_amount) == (Decimal("2"), Decimal("1"), None)
    assert split.provider_id == prov.id and split.observed_at == NOW and split.provider_raw == {"kind": "SPLIT", "value": 2.0}
    assert "does not distinguish a bonus" in split.notes
    div = db.scalar(select(CorporateAction).where(CorporateAction.action_type == CorporateActionType.DIVIDEND))
    assert div.cash_amount == Decimal("5.5") and div.ratio_numerator is None


def test_recording_the_same_actions_again_is_idempotent(db, ctx):
    inst, prov, nb = ctx
    a = [RawCorporateAction(date(2024, 10, 28), "SPLIT", 2.0)]
    kw = dict(instrument_id=inst.id, provider_id=prov.id, source="yahoo", observed_at=NOW, actions=a)
    archive.record_corporate_actions(db, batch_id=nb(), **kw)
    n, issues = archive.record_corporate_actions(db, batch_id=nb(), **kw)
    db.commit()
    assert n == 0 and not issues and db.scalar(select(func.count()).select_from(CorporateAction)) == 1


def test_a_revised_corporate_action_is_reported_not_applied(db, ctx):
    inst, prov, nb = ctx
    kw = dict(instrument_id=inst.id, provider_id=prov.id, source="yahoo", observed_at=NOW)
    archive.record_corporate_actions(db, batch_id=nb(), actions=[RawCorporateAction(date(2025, 6, 5), "DIVIDEND", 5.5)], **kw)
    n, issues = archive.record_corporate_actions(db, batch_id=nb(), actions=[RawCorporateAction(date(2025, 6, 5), "DIVIDEND", 6.0)], **kw)
    db.commit()
    assert n == 0 and [i.issue_type for i in issues] == [IssueType.RESTATEMENT] and issues[0].details["archived"] == "5.500000"
    assert db.scalar(select(CorporateAction.cash_amount)) == Decimal("5.500000")


# ---- raw / adjusted separation ---------------------------------------------------------------------------


def archive_day(db, ctx, observed):
    inst, prov, nb = ctx
    archive.archive_candles(db, candles_for(inst, day_bars(D, last=4), observed=observed), provider_id=prov.id, batch_id=nb())
    db.commit()
    return inst, prov


def add_split(db, ctx, ex_date, ratio=2.0):
    inst, prov, nb = ctx
    archive.record_corporate_actions(db, instrument_id=inst.id, provider_id=prov.id, source="yahoo", batch_id=nb(), observed_at=NOW, actions=[RawCorporateAction(ex_date, "SPLIT", ratio)])
    db.commit()


def raw_snapshot(db):
    return [(c.id, c.open, c.high, c.low, c.close, c.volume) for c in db.scalars(select(Candle).order_by(Candle.id))]


def test_adjusted_rows_for_a_split_after_capture_are_scaled_and_stored_separately(db, ctx):
    inst, prov = archive_day(db, ctx, observed=datetime(2026, 11, 10, 11, 0, tzinfo=UTC))  # captured on 10 Nov
    add_split(db, ctx, date(2026, 11, 20))  # split takes effect later: bars were captured pre-split
    before = raw_snapshot(db)
    rep = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    assert rep.splits_considered == 1 and rep.adjusted_rows_written == 4 and rep.skipped_ambiguous == 0
    assert raw_snapshot(db) == before  # raw prices were not touched
    adj = db.scalars(select(AdjustedCandle).join(Candle).order_by(Candle.id)).all()
    raw = db.scalars(select(Candle).order_by(Candle.id)).all()
    for a, r in zip(adj, raw):
        assert a.adjustment_factor == Decimal("0.5") and a.adj_close == (r.close / 2).quantize(Decimal("0.000001"))
        assert a.adj_volume == r.volume * 2 and a.adjustment_method is AdjustmentMethod.SPLIT
        assert a.corporate_action_set_version == rep.action_set_version and a.adjustment_source == "agentx-derived"


def test_a_bar_captured_after_the_ex_date_is_not_adjusted_because_the_provider_may_already_have_done_so(db, ctx):
    inst, prov = archive_day(db, ctx, observed=datetime(2026, 11, 25, 11, 0, tzinfo=UTC))  # captured after the split
    add_split(db, ctx, date(2026, 11, 20))
    rep = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    assert rep.skipped_ambiguous == 4 and rep.adjusted_rows_written == 0
    assert db.scalar(select(func.count()).select_from(AdjustedCandle)) == 0  # unknown is recorded as unknown, not guessed
    assert len(rep.skipped_candle_ids_sample) == 4


def test_a_split_before_the_bar_does_not_adjust_it(db, ctx):
    inst, prov = archive_day(db, ctx, observed=datetime(2026, 11, 10, 11, 0, tzinfo=UTC))
    add_split(db, ctx, date(2026, 11, 2))  # before the bars' session date: already on the post-split basis
    rep = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    assert rep.unchanged_rows_written == 4 and rep.adjusted_rows_written == 0
    assert {a.adjustment_factor for a in db.scalars(select(AdjustedCandle))} == {Decimal("1")}


def test_no_corporate_actions_gives_factor_one_rows_tagged_with_the_empty_set(db, ctx):
    inst, prov = archive_day(db, ctx, observed=NOW)
    rep = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    assert rep.splits_considered == 0 and rep.action_set_version.startswith("splits-0-")
    assert db.scalar(select(func.count()).select_from(AdjustedCandle)) == 4


def test_adjustment_is_idempotent_per_action_set_and_versioned_when_actions_change(db, ctx):
    inst, prov = archive_day(db, ctx, observed=datetime(2026, 11, 10, 11, 0, tzinfo=UTC))
    add_split(db, ctx, date(2026, 11, 20))
    first = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    second = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    assert first.action_set_version == second.action_set_version and second.adjusted_rows_written == 0
    assert db.scalar(select(func.count()).select_from(AdjustedCandle)) == 4
    add_split(db, ctx, date(2026, 12, 1), ratio=5.0)  # a new action changes the action-set fingerprint
    third = compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    assert third.action_set_version != first.action_set_version and third.adjusted_rows_written == 4
    assert db.scalar(select(func.count()).select_from(AdjustedCandle)) == 8  # both computations are kept
    assert {a.adjustment_factor for a in db.scalars(select(AdjustedCandle).where(AdjustedCandle.corporate_action_set_version == third.action_set_version))} == {Decimal("0.1")}


def test_the_raw_candle_table_has_no_adjusted_columns_and_the_adjusted_table_cannot_alter_raw(db, ctx):
    assert not any(c.startswith("adj") for c in Candle.__table__.c.keys())
    inst, prov = archive_day(db, ctx, observed=NOW)
    compute_split_adjusted(db, instrument_id=inst.id, provider_id=prov.id, timeframe=Timeframe.M5)
    db.commit()
    c = db.scalars(select(Candle)).first()
    c.close = c.close + 1
    with pytest.raises((IntegrityError, DBAPIError)):
        db.commit()
    db.rollback()
