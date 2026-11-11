"""Migration 0009: immutability triggers, one active run per user, exact NUMERIC storage and the downgrade guard."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.backtest import PRICE_BASIS
from app.backtest.costs import CostBasis
from app.backtest.model import CapitalSource, EventType
from app.db.enums import BacktestRunStatus
from app.db.models import BacktestEvent, BacktestRun, User
from tests.conftest import alembic_config
from tests.integration.backtest_support import instrument

T = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)


def user(db, name="a") -> User:
    u = User(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", password_hash="x")
    db.add(u)
    db.commit()
    return u


def run(db, owner, status=BacktestRunStatus.QUEUED, **over) -> BacktestRun:
    fields = dict(
        user_id=owner.id, status=status, request={"submitted": {}}, instrument_ids=[1], requested_start=date(2026, 11, 4),
        requested_end=date(2026, 11, 10), selection_mode="PINNED", capital=Decimal("100000.00"),
        capital_source=CapitalSource.ACCOUNT_INITIAL_CAPITAL, cost_basis=CostBasis.GROSS_OF_COSTS, price_basis=PRICE_BASIS,
        engine_version="1", fill_model="NEXT_BAR_OPEN_V1", sizing_model="WHOLE_SHARES_FULL_ALLOCATION_V1", config_fingerprint="a" * 64,
        disclosures=["x"],
    )
    fields.update(over)
    r = BacktestRun(**fields)
    db.add(r)
    db.commit()
    return r


def rejected(db, fn):
    with pytest.raises((IntegrityError, DBAPIError)):
        fn()
        db.commit()
    db.rollback()


def event(r, inst, owner_id=None, seq=1) -> BacktestEvent:
    return BacktestEvent(
        run_id=r.id, user_id=owner_id or r.user_id, instrument_id=inst.id, seq=seq, bar_start=T, session_date=date(2026, 11, 4),
        event_type=EventType.MISSING_BARS, detail={},
    )


def set_status(db, r, status, **extra):
    r.status = status
    for k, v in extra.items():
        setattr(r, k, v)
    db.commit()


def test_one_active_run_per_user_but_users_are_independent(db):
    a, b = user(db, "a"), user(db, "b")
    run(db, a)
    rejected(db, lambda: run(db, a))  # a second QUEUED run for the same user
    run(db, b)  # another user is not blocked
    first = db.query(BacktestRun).filter_by(user_id=a.id).one()
    set_status(db, first, BacktestRunStatus.FAILED)
    run(db, a)  # a finished run frees the slot


def test_the_request_configuration_and_owner_never_change(db):
    a, b = user(db, "a"), user(db, "b")
    r = run(db, a)
    for column, value in (("request", {"x": 1}), ("config_fingerprint", "b" * 64), ("capital", Decimal(1)), ("user_id", b.id),
                          ("cost_basis", CostBasis.NET_OF_MODELLED_COSTS), ("disclosures", [])):
        rejected(db, lambda c=column, v=value: setattr(r, c, v))
        db.refresh(r)


def test_status_only_moves_forward_and_finished_runs_are_frozen(db):
    a = user(db)
    r = run(db, a)
    rejected(db, lambda: set_status(db, r, BacktestRunStatus.SUCCEEDED, data_fingerprint="d" * 64, result_fingerprint="r" * 64))
    db.refresh(r)
    set_status(db, r, BacktestRunStatus.RUNNING)
    rejected(db, lambda: set_status(db, r, BacktestRunStatus.QUEUED))
    db.refresh(r)
    rejected(db, lambda: set_status(db, r, BacktestRunStatus.RUNNING, summary={"x": 1}))  # results only with SUCCEEDED
    db.refresh(r)
    set_status(db, r, BacktestRunStatus.SUCCEEDED, data_fingerprint="d" * 64, result_fingerprint="r" * 64, summary={"ok": True})
    rejected(db, lambda: set_status(db, r, BacktestRunStatus.SUCCEEDED, error_message="edited"))
    db.refresh(r)
    rejected(db, lambda: db.delete(r))


def test_fingerprints_exist_exactly_for_succeeded_runs(db):
    a = user(db)
    rejected(db, lambda: run(db, a, data_fingerprint="d" * 64, result_fingerprint="r" * 64))
    r = run(db, a, status=BacktestRunStatus.RUNNING)
    rejected(db, lambda: set_status(db, r, BacktestRunStatus.SUCCEEDED))


def test_result_rows_are_insert_only_while_running_and_owner_bound(db):
    a, b = user(db, "a"), user(db, "b")
    inst = instrument(db)
    r = run(db, a)
    rejected(db, lambda: db.add(event(r, inst)))  # QUEUED: no results yet
    set_status(db, r, BacktestRunStatus.RUNNING)
    rejected(db, lambda: db.add(event(r, inst, owner_id=b.id)))  # another user's id on a's run
    db.add(event(r, inst))
    db.commit()
    e = db.query(BacktestEvent).one()
    rejected(db, lambda: setattr(e, "config_id", "changed"))
    db.refresh(e)
    rejected(db, lambda: db.delete(e))
    set_status(db, r, BacktestRunStatus.SUCCEEDED, data_fingerprint="d" * 64, result_fingerprint="r" * 64)
    rejected(db, lambda: db.add(event(r, inst, seq=2)))  # finished: no more results


def test_decimals_are_stored_exactly(db):
    a = user(db)
    exact = Decimal("100000.123456789012345678901234567")
    r = run(db, a, capital=exact)
    db.expire_all()
    assert db.get(BacktestRun, r.id).capital == exact


def test_downgrading_0009_is_refused_while_runs_exist(engine, db):
    from tests.integration.test_exchange import _head

    run(db, user(db))
    with pytest.raises(RuntimeError, match="cannot downgrade 0009"):
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0008")
    with engine.connect() as conn:
        # nothing was undone: later revisions (e.g. 0010) roll back with the refused transaction
        assert conn.execute(text("select version_num from alembic_version")).scalar() == _head()
        assert conn.execute(text("select count(*) from backtest_runs")).scalar() == 1
