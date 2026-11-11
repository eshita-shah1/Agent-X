"""Phase 2 schema guarantees: registry safety rails, candle versioning, provenance and constraints."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

import app.db.models as models
from app.db.base import Base
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
    Candle,
    DataIngestionBatch,
    DataIngestionJob,
    DataProvider,
    ExchangeCalendarDay,
    Instrument,
    InstrumentProviderSymbol,
)
from tests.conftest import alembic_config

T0 = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)


def rejected(db: Session, fn):
    with pytest.raises((IntegrityError, DBAPIError)) as info:
        fn()
        db.commit()
    db.rollback()
    return str(info.value)


@pytest.fixture
def base(db):
    provider = DataProvider(code="t", name="T")
    inst = Instrument(symbol="ABC", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")
    db.add_all([provider, inst])
    db.commit()
    job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=provider.id, timeframe=Timeframe.M5)
    db.add(job)
    db.commit()
    batch = DataIngestionBatch(
        job_id=job.id, instrument_id=inst.id, provider_id=provider.id, timeframe=Timeframe.M5, provider_symbol="ABC.NS",
        request_start=T0, request_end=T0 + timedelta(days=1), status=BatchStatus.SUCCEEDED,
    )
    db.add(batch)
    db.commit()
    return provider, inst, batch


def candle(base, **over):
    provider, inst, batch = base
    f = dict(
        instrument_id=inst.id, provider_id=provider.id, timeframe=Timeframe.M5, bar_start_ts=T0, bar_end_ts=T0 + timedelta(minutes=5),
        session_date=date(2026, 11, 9), open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100.5"), volume=10,
        is_final=True, observed_at=T0 + timedelta(hours=1), provider_symbol="ABC.NS", ingestion_batch_id=batch.id,
    )
    f.update(over)
    return Candle(**f)


# ---- instrument registry rails ---------------------------------------------------------------------


def test_a_futures_instrument_cannot_be_enabled(db):
    msg = rejected(db, lambda: db.add(Instrument(symbol="NIFTY", exchange="NSE", instrument_type=InstrumentType.FUTURE, timezone="Asia/Kolkata", trading_status=TradingStatus.ENABLED)))
    assert "futures_must_be_disabled" in msg


def test_a_disabled_futures_placeholder_is_allowed_with_a_reason(db):
    db.add(Instrument(symbol="NIFTY", exchange="NSE", instrument_type=InstrumentType.FUTURE, timezone="Asia/Kolkata", trading_status=TradingStatus.DISABLED, disabled_reason="no contract data"))
    db.commit()


def test_a_disabled_instrument_must_state_why(db):
    msg = rejected(db, lambda: db.add(Instrument(symbol="X", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata", trading_status=TradingStatus.DISABLED)))
    assert "disabled_requires_reason" in msg


def test_an_existing_futures_row_cannot_be_enabled_later(db):
    f = Instrument(symbol="BANKNIFTY", exchange="NSE", instrument_type=InstrumentType.FUTURE, timezone="Asia/Kolkata", trading_status=TradingStatus.DISABLED, disabled_reason="r")
    db.add(f)
    db.commit()
    f.trading_status, f.disabled_reason = TradingStatus.ENABLED, None
    assert "futures_must_be_disabled" in rejected(db, lambda: None)


def test_equities_cannot_carry_contract_fields(db):
    msg = rejected(db, lambda: db.add(Instrument(symbol="X", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata", expiry_date=date(2026, 12, 31))))
    assert "equity_has_no_contract" in msg


def test_futures_rows_can_hold_a_contract_identifier_and_expiry_for_the_future(db):
    db.add(Instrument(symbol="NIFTY", exchange="NSE", instrument_type=InstrumentType.FUTURE, timezone="Asia/Kolkata", trading_status=TradingStatus.DISABLED, disabled_reason="r", contract_identifier="NIFTY-X", expiry_date=date(2026, 12, 31)))
    db.commit()


def test_instrument_defaults_never_invent_contract_specifications(db):
    i = Instrument(symbol="Y", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")
    db.add(i)
    db.commit()
    db.refresh(i)
    assert (i.tick_size, i.lot_size, i.contract_identifier, i.expiry_date) == (None, None, None, None)
    assert i.currency == "INR" and i.trading_status is TradingStatus.ENABLED


def test_provider_symbol_mapping_is_unique_per_instrument_and_per_provider_symbol(db, base):
    provider, inst, _ = base
    db.add(InstrumentProviderSymbol(instrument_id=inst.id, provider_id=provider.id, provider_symbol="ABC.NS"))
    db.commit()
    other = Instrument(symbol="DEF", exchange="NSE", instrument_type=InstrumentType.EQUITY, timezone="Asia/Kolkata")
    db.add(other)
    db.commit()
    assert "uq_instrument_provider_symbols_instrument_provider" in rejected(db, lambda: db.add(InstrumentProviderSymbol(instrument_id=inst.id, provider_id=provider.id, provider_symbol="ZZ.NS")))
    assert "uq_instrument_provider_symbols_provider_symbol" in rejected(db, lambda: db.add(InstrumentProviderSymbol(instrument_id=other.id, provider_id=provider.id, provider_symbol="ABC.NS")))


def test_new_symbol_mappings_start_unverified(db, base):
    provider, inst, _ = base
    m = InstrumentProviderSymbol(instrument_id=inst.id, provider_id=provider.id, provider_symbol="ABC.NS")
    db.add(m)
    db.commit()
    db.refresh(m)
    assert m.verification_status is VerificationStatus.UNVERIFIED and m.verified_at is None


# ---- candle provenance, versioning and immutability ---------------------------------------------------


def test_candles_require_provenance(db, base):
    assert Candle.__table__.c.provider_symbol.nullable is False
    assert Candle.__table__.c.ingestion_batch_id.nullable is False
    db.add(candle(base))
    db.commit()
    c = db.query(Candle).one()
    assert c.provider_symbol == "ABC.NS" and c.ingestion_batch_id and c.observation_version == 1 and c.is_canonical is True
    assert c.source_metadata == {}


def test_only_finalized_candles_can_be_archived(db, base):
    assert "only_finalized_archived" in rejected(db, lambda: db.add(candle(base, is_final=False)))


def test_a_second_canonical_version_of_a_bar_is_rejected(db, base):
    db.add(candle(base))
    db.commit()
    msg = rejected(db, lambda: db.add(candle(base, observation_version=2, close=Decimal("100.6"))))
    assert "uq_candles_canonical" in msg


def test_a_restated_non_canonical_version_can_sit_alongside_the_original(db, base):
    db.add(candle(base))
    db.add(candle(base, observation_version=2, is_canonical=False, close=Decimal("100.6")))
    db.commit()
    rows = db.query(Candle).order_by(Candle.observation_version).all()
    assert [(r.observation_version, r.is_canonical) for r in rows] == [(1, True), (2, False)]


def test_duplicate_observation_version_is_rejected(db, base):
    db.add(candle(base))
    db.commit()
    assert "uq_candles_observation" in rejected(db, lambda: db.add(candle(base, is_canonical=False)))


def test_observation_version_must_be_positive(db, base):
    assert "version_positive" in rejected(db, lambda: db.add(candle(base, observation_version=0)))


@pytest.mark.parametrize("column,value", [("provider_symbol", "'EVIL.NS'"), ("is_canonical", "false"), ("observation_version", "9"), ("source_metadata", "jsonb_build_object('x', 1)"), ("ingestion_batch_id", "ingestion_batch_id + 1")])
def test_the_new_provenance_and_version_columns_are_immutable(db, base, column, value):
    db.add(candle(base))
    db.commit()
    msg = rejected(db, lambda: db.execute(text(f"update candles set {column} = {value}")))
    assert "immutable" in msg or "foreign key" in msg.lower()


def test_the_verification_label_remains_the_only_mutable_part(db, base):
    c = candle(base)
    db.add(c)
    db.commit()
    c.price_basis, c.basis_verified_by = c.price_basis.__class__("AS_TRADED_VERIFIED"), "probe-x"
    db.commit()


def test_candle_requires_an_existing_ingestion_batch(db, base):
    assert "foreign key" in rejected(db, lambda: db.add(candle(base, ingestion_batch_id=999999))).lower()


# ---- jobs, calendar, enums ----------------------------------------------------------------------------------


def _job(provider, status):
    return DataIngestionJob(job_type=JobType.BACKFILL, status=status, provider_id=provider.id, timeframe=Timeframe.M5)


def test_the_database_allows_only_one_active_ingestion_job(db, base):
    provider = base[0]
    db.add(_job(provider, JobStatus.RUNNING))
    db.commit()
    assert "uq_ingestion_jobs_single_active" in rejected(db, lambda: db.add(_job(provider, JobStatus.QUEUED)))
    assert "uq_ingestion_jobs_single_active" in rejected(db, lambda: db.add(_job(provider, JobStatus.RUNNING)))


def test_finished_jobs_do_not_block_new_ones(db, base):
    provider = base[0]
    for s in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.INTERRUPTED, JobStatus.PARTIAL, JobStatus.SUCCEEDED):
        db.add(_job(provider, s))
    db.commit()
    db.add(_job(provider, JobStatus.QUEUED))
    db.commit()


def test_special_session_needs_valid_times(db):
    day = date(2026, 11, 14)
    assert "special_session_has_times" in rejected(db, lambda: db.add(ExchangeCalendarDay(exchange="NSE", day=day, kind=CalendarDayKind.SPECIAL_SESSION, source="t")))
    assert "special_session_has_times" in rejected(db, lambda: db.add(ExchangeCalendarDay(exchange="NSE", day=day, kind=CalendarDayKind.SPECIAL_SESSION, open_time=time(11), close_time=time(9), source="t")))
    db.add(ExchangeCalendarDay(exchange="NSE", day=day, kind=CalendarDayKind.SPECIAL_SESSION, open_time=time(9, 15), close_time=time(11, 15), source="t"))
    db.add(ExchangeCalendarDay(exchange="NSE", day=date(2026, 11, 10), kind=CalendarDayKind.HOLIDAY, source="t"))
    db.commit()


def test_calendar_days_are_unique_per_exchange_and_day(db):
    db.add(ExchangeCalendarDay(exchange="NSE", day=date(2026, 11, 10), kind=CalendarDayKind.HOLIDAY, source="t"))
    db.commit()
    assert "uq_exchange_calendar_days_exchange_day" in rejected(db, lambda: db.add(ExchangeCalendarDay(exchange="NSE", day=date(2026, 11, 10), kind=CalendarDayKind.HOLIDAY, source="t2")))


def test_the_calendar_table_ships_empty_no_holidays_are_invented(db):
    assert db.query(ExchangeCalendarDay).count() == 0


def _enum_columns():
    from sqlalchemy import Enum

    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, Enum) and col.type.enum_class is not None:
                yield table.name, col.name, col.type.enum_class


def test_every_enum_column_has_a_check_constraint_that_matches_the_python_enum(db):
    """Alembic does not diff CHECK contents, so the migration's literal value lists are verified here."""
    checked = 0
    for table, column, enum_class in _enum_columns():
        defs = [r[0] for r in db.execute(text("select pg_get_constraintdef(oid) from pg_constraint where conrelid = cast(:t as regclass) and contype = 'c'"), {"t": table})]
        mine = [d for d in defs if f"({column})" in d or f"{column}::text" in d]
        assert mine, f"no CHECK constraint found for {table}.{column}"
        for member in enum_class:
            assert f"'{member.value}'" in " ".join(mine), f"{table}.{column} CHECK is missing {member.value!r}"
        checked += 1
    assert checked >= 15


def test_every_table_is_in_the_orm_metadata_and_nothing_else_exists(db):
    in_db = {r[0] for r in db.execute(text("select tablename from pg_tables where schemaname='public'"))} - {"alembic_version"}
    assert in_db == {t.name for t in Base.metadata.sorted_tables}


def test_downgrading_0003_is_refused_once_candles_exist(engine, db, base):
    db.add(candle(base))
    db.commit()
    with pytest.raises(RuntimeError, match="0003"):
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0002")
    assert db.query(Candle).count() == 1  # nothing was lost


_P = "insert into data_providers(code,name) values ('p','P')"
_I = "insert into instruments(symbol,exchange,instrument_type,timezone) values ('A','NSE','EQUITY','Asia/Kolkata')"
_J = "insert into data_ingestion_jobs(id,job_type,status,provider_id,timeframe) select gen_random_uuid(),'BACKFILL','SUCCEEDED',id,'5m' from data_providers"
_B = (
    "insert into data_ingestion_batches(job_id,instrument_id,provider_id,timeframe,provider_symbol,request_start,request_end,status) "
    "select j.id,i.id,j.provider_id,'5m','A.NS',now(),now(),'SUCCEEDED' from data_ingestion_jobs j, instruments i"
)


@pytest.mark.parametrize(
    "holding,setup",
    [
        ("data_ingestion_jobs", [_P, _J]),
        ("data_ingestion_batches", [_P, _I, _J, _B]),
        ("data_quality_issues", [_P, _I, _J, _B, "insert into data_quality_issues(batch_id,instrument_id,issue_type,severity,message) select id,instrument_id,'OUT_OF_SESSION','WARNING','m' from data_ingestion_batches"]),
        ("corporate_actions", [_P, _I, "insert into corporate_actions(instrument_id,action_type,ex_date,cash_amount,source,provider_id,observed_at) select i.id,'DIVIDEND','2026-01-01',5,'t',p.id,now() from instruments i, data_providers p"]),
        ("exchange_calendar_days", ["insert into exchange_calendar_days(exchange,day,kind,source) values ('NSE','2026-01-26','HOLIDAY','t')"]),
        ("instrument_provider_symbols", [_P, _I, "insert into instrument_provider_symbols(instrument_id,provider_id,provider_symbol) select i.id,p.id,'A.NS' from instruments i, data_providers p"]),
        ("provider_capabilities", [_P, "insert into provider_capabilities(provider_id,capability,status) select id,'LATEST_CANDLE','VERIFIED' from data_providers"]),
        ("provider_health_state", [_P, "insert into provider_health_state(provider_id) select id from data_providers"]),
        ("instruments (registry fields)", ["insert into instruments(symbol,exchange,instrument_type,timezone,trading_status,disabled_reason) values ('NIFTY','NSE','FUTURE','Asia/Kolkata','DISABLED','no contract data')"]),
    ],
)
def test_downgrading_0003_is_refused_while_any_market_data_would_be_discarded(engine, holding, setup):
    with engine.begin() as conn:
        for sql in setup:
            conn.execute(text(sql))
    table = holding.split(" ")[0]
    with engine.connect() as conn:
        before = conn.execute(text(f"select count(*) from {table}")).scalar()
    with pytest.raises(RuntimeError, match=re.escape(holding)):
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0002")
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0003"
        assert conn.execute(text(f"select count(*) from {table}")).scalar() == before > 0  # nothing was discarded


def test_phase1_compatible_rows_do_not_block_a_downgrade(engine):
    """The guard is not over-broad: an ordinary enabled INR equity and a provider lose nothing in a downgrade."""
    with engine.begin() as conn:
        conn.execute(text(_P))
        conn.execute(text(_I))
    try:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0002")
        with engine.connect() as conn:
            assert conn.execute(text("select count(*) from instruments")).scalar() == 1
    finally:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")


def test_upgrading_0003_is_refused_if_candles_already_exist(engine):
    """The NOT NULL provenance columns cannot be back-filled honestly."""
    from tests.conftest import BACKEND_DIR  # noqa: F401

    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0002")
    try:
        with engine.begin() as conn:
            conn.execute(text("insert into data_providers(code,name) values ('x','x')"))
            conn.execute(text("insert into instruments(symbol,exchange,instrument_type,timezone) values ('A','NSE','EQUITY','Asia/Kolkata')"))
            conn.execute(text("insert into candles(instrument_id,provider_id,timeframe,bar_start_ts,bar_end_ts,session_date,open,high,low,close,volume,is_final,observed_at) select i.id,p.id,'5m',now(),now()+interval '5 minutes',current_date,1,2,1,1.5,1,true,now() from instruments i, data_providers p"))
        with pytest.raises(RuntimeError, match="provenance cannot be invented"):
            with engine.begin() as conn:
                command.upgrade(alembic_config(conn), "head")
    finally:
        with engine.begin() as conn:
            conn.execute(text("truncate candles, instruments, data_providers restart identity cascade"))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
