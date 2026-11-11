"""Schema-level guarantees: raw price immutability, raw/adjusted separation, and data constraints."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import AdjustmentMethod, CorporateActionType, InstrumentType, PriceBasis, Timeframe
from app.db.models import AdjustedCandle, AuditEvent, Candle, CorporateAction, DataProvider, Instrument

T0 = datetime(2026, 1, 5, 4, 0, tzinfo=UTC)  # 09:30 IST


@pytest.fixture
def base(db):
    provider = DataProvider(code="test", name="Test provider")
    inst = Instrument(symbol="TESTCO", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")
    db.add_all([provider, inst])
    db.commit()
    return provider, inst


def make_candle(base, **over) -> Candle:
    provider, inst = base
    fields = dict(
        instrument_id=inst.id,
        provider_id=provider.id,
        timeframe=Timeframe.M5,
        bar_start_ts=T0,
        bar_end_ts=T0 + timedelta(minutes=5),
        session_date=date(2026, 1, 5),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=1000,
        is_final=True,
        observed_at=T0 + timedelta(minutes=20),
    )
    fields.update(over)
    return Candle(**fields)


def insert(db, obj):
    db.add(obj)
    db.commit()
    return obj


def expect_rejected(db, fn):
    with pytest.raises((IntegrityError, DBAPIError)):
        fn()
        db.commit()
    db.rollback()


# ---- candle validity -----------------------------------------------------------------------


def test_valid_candle_defaults_to_unverified_basis(db, base):
    c = insert(db, make_candle(base))
    db.refresh(c)
    assert c.price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE
    assert c.basis_verified_by is None
    assert c.ingested_at is not None


@pytest.mark.parametrize(
    "bad",
    [
        dict(high=Decimal("98")),  # high below low
        dict(low=Decimal("101.5")),  # low above open
        dict(open=Decimal("105")),  # open above high
        dict(close=Decimal("98")),  # close below low
        dict(low=Decimal("0"), open=Decimal("0.0"), close=Decimal("0.0")),  # non-positive
        dict(volume=-1),
        dict(bar_end_ts=T0),  # empty interval
        dict(bar_end_ts=T0 - timedelta(minutes=5)),
    ],
)
def test_invalid_candles_are_rejected(db, base, bad):
    expect_rejected(db, lambda: db.add(make_candle(base, **bad)))


def test_duplicate_candle_rejected(db, base):
    insert(db, make_candle(base))
    expect_rejected(db, lambda: db.add(make_candle(base)))


def test_same_bar_from_a_different_provider_is_a_separate_row(db, base):
    insert(db, make_candle(base))
    other = insert(db, DataProvider(code="other", name="Other"))
    insert(db, make_candle(base, provider_id=other.id))  # no provider is silently merged or replaced


# ---- raw immutability ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,value",
    [
        ("open", "100.5"),
        ("high", "110"),
        ("low", "98"),
        ("close", "100.5"),
        ("volume", "5"),
        ("is_final", "false"),
        ("observed_at", "now()"),
        ("session_date", "'2026-01-06'"),
        ("bar_start_ts", "bar_start_ts + interval '1 minute'"),
    ],
)
def test_raw_candle_columns_cannot_be_updated(db, base, column, value):
    c = insert(db, make_candle(base))
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        db.execute(text(f"update candles set {column} = {value} where id = :i"), {"i": c.id})
        db.commit()
    db.rollback()
    assert "immutable" in str(exc.value) or "check" in str(exc.value).lower()


def test_updating_prices_through_the_orm_is_also_blocked(db, base):
    c = insert(db, make_candle(base))
    c.close = Decimal("100.25")
    expect_rejected(db, lambda: None)
    db.expire_all()
    assert db.get(Candle, c.id).close == Decimal("101")


def test_price_basis_can_be_relabelled_only_with_evidence(db, base):
    c = insert(db, make_candle(base))
    c.price_basis = PriceBasis.AS_TRADED_VERIFIED  # no verifier recorded
    expect_rejected(db, lambda: None)

    c = db.get(Candle, c.id)
    c.price_basis = PriceBasis.AS_TRADED_VERIFIED
    c.basis_verified_by = "probe-run-0001"
    db.commit()
    db.expire_all()
    fresh = db.get(Candle, c.id)
    assert fresh.price_basis is PriceBasis.AS_TRADED_VERIFIED
    assert fresh.close == Decimal("101")  # prices untouched by relabelling


def test_cannot_insert_a_verified_label_without_evidence(db, base):
    expect_rejected(db, lambda: db.add(make_candle(base, price_basis=PriceBasis.SPLIT_ADJUSTED_VERIFIED)))


# ---- raw/adjusted separation ---------------------------------------------------------------


def make_adjusted(candle: Candle, **over) -> AdjustedCandle:
    fields = dict(
        candle_id=candle.id,
        adjustment_method=AdjustmentMethod.SPLIT,
        adjustment_source="unit-test",
        corporate_action_set_version="v1",
        adjustment_factor=Decimal("0.5"),
        adj_open=Decimal("50"),
        adj_high=Decimal("51"),
        adj_low=Decimal("49.5"),
        adj_close=Decimal("50.5"),
        adj_volume=2000,
    )
    fields.update(over)
    return AdjustedCandle(**fields)


def test_adjusted_prices_live_in_a_separate_table_and_leave_raw_untouched(db, base):
    c = insert(db, make_candle(base))
    insert(db, make_adjusted(c))
    db.expire_all()
    raw = db.get(Candle, c.id)
    assert (raw.open, raw.high, raw.low, raw.close, raw.volume) == (
        Decimal("100"),
        Decimal("102"),
        Decimal("99"),
        Decimal("101"),
        1000,
    )
    assert "adj_open" not in Candle.__table__.c
    assert "adjustment_factor" not in Candle.__table__.c


def test_adjusted_candle_requires_provenance_columns():
    for col in ("adjustment_method", "adjustment_source", "corporate_action_set_version", "adjustment_factor"):
        assert not AdjustedCandle.__table__.c[col].nullable


def test_adjusted_candle_identity_is_unique_per_method_and_action_set(db, base):
    c = insert(db, make_candle(base))
    insert(db, make_adjusted(c))
    expect_rejected(db, lambda: db.add(make_adjusted(c)))
    insert(db, make_adjusted(c, corporate_action_set_version="v2"))  # a new action-set version is a new row


@pytest.mark.parametrize(
    "bad",
    [dict(adjustment_factor=Decimal("0")), dict(adj_high=Decimal("40")), dict(adj_low=Decimal("0")), dict(adj_volume=-1)],
)
def test_invalid_adjusted_candles_rejected(db, base, bad):
    c = insert(db, make_candle(base))
    expect_rejected(db, lambda: db.add(make_adjusted(c, **bad)))


def test_candle_with_adjusted_rows_cannot_be_deleted(db, base):
    c = insert(db, make_candle(base))
    insert(db, make_adjusted(c))
    expect_rejected(db, lambda: db.delete(c))


# ---- corporate actions ---------------------------------------------------------------------


def test_split_requires_a_positive_ratio(db, base):
    _, inst = base
    expect_rejected(
        db,
        lambda: db.add(
            CorporateAction(instrument_id=inst.id, action_type=CorporateActionType.SPLIT, ex_date=date(2026, 1, 1), source="t")
        ),
    )
    insert(
        db,
        CorporateAction(
            instrument_id=inst.id,
            action_type=CorporateActionType.SPLIT,
            ex_date=date(2026, 1, 1),
            ratio_numerator=Decimal("1"),
            ratio_denominator=Decimal("2"),
            source="t",
        ),
    )


def test_dividend_requires_a_cash_amount(db, base):
    _, inst = base
    expect_rejected(
        db,
        lambda: db.add(
            CorporateAction(instrument_id=inst.id, action_type=CorporateActionType.DIVIDEND, ex_date=date(2026, 1, 1), source="t")
        ),
    )
    insert(
        db,
        CorporateAction(
            instrument_id=inst.id,
            action_type=CorporateActionType.DIVIDEND,
            ex_date=date(2026, 1, 1),
            cash_amount=Decimal("5"),
            source="t",
        ),
    )


# ---- instruments / audit -------------------------------------------------------------------


def test_instrument_identity_is_unique_and_metadata_is_not_defaulted(db, base):
    _, inst = base
    assert inst.tick_size is None and inst.lot_size is None  # never invented
    expect_rejected(
        db,
        lambda: db.add(Instrument(symbol="TESTCO", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")),
    )


def test_audit_events_are_append_only(db):
    ev = AuditEvent(event_type="test.event")
    insert(db, ev)
    with pytest.raises((IntegrityError, DBAPIError)):
        db.execute(text("update audit_events set event_type = 'x'"))
        db.commit()
    db.rollback()
    with pytest.raises((IntegrityError, DBAPIError)):
        db.execute(text("delete from audit_events"))
        db.commit()
    db.rollback()
    assert db.scalar(text("select count(*) from audit_events")) == 1
