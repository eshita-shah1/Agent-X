"""Mock Exchange + risk pipeline against a real PostgreSQL, with deterministic fixtures."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import OrderSide, OrderStatus, OrderType, PriceBasis, RiskOutcome
from app.db.models import AuditEvent, CashLedgerEntry, Fill, Order, Position, RiskDecision, RiskState
from app.exchange.service import OrderNotCancellable, OrderNotFound
from app.orders.service import IdempotencyConflict, InvalidOrder, OrderService, TradingContext
from tests.integration.trading_support import (
    NOW,
    Clock,
    configure_risk,
    enabled_settings,
    intent,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    submit,
)

D = Decimal


@pytest.fixture
def env(db, test_settings):
    provider = make_provider(db)
    inst = make_instrument(db)
    account = make_account(db)
    configure_risk(db, account)
    return dict(
        db=db,
        provider=provider,
        inst=inst,
        account=account,
        settings=enabled_settings(test_settings),
        clock=Clock(),
    )


def go(env, order_intent, settings=None, clock=None):
    return submit(env["db"], settings or env["settings"], clock or env["clock"], env["account"], order_intent)


def ctx(env, settings=None):
    return TradingContext.build(env["db"], settings or env["settings"], env["clock"])


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def cash(env) -> Decimal:
    return ctx(env).portfolio.cash_balance(env["account"])


# ---- default configuration: decision D4 blocks fills ----------------------------------------


def test_default_configuration_blocks_every_fill(env, test_settings):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"]), settings=test_settings)  # untouched defaults
    assert r.order.status is OrderStatus.REJECTED
    assert "NO_ELIGIBLE_PRICE" in r.order.reject_reason and "SOURCE_DISABLED" in r.order.reject_reason
    assert r.decision.outcome is RiskOutcome.REJECTED
    assert count(env["db"], Fill) == 0 and count(env["db"], CashLedgerEntry) == 0


def test_unverified_provider_native_candle_is_never_an_execution_price(env):
    make_candle(env["db"], env["provider"], env["inst"], "100", verified=False)
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED
    assert "UNVERIFIED_BASIS" in r.order.reject_reason
    assert count(env["db"], Fill) == 0


def test_stale_observation_blocks_new_orders(env):
    make_candle(env["db"], env["provider"], env["inst"], "100", end=NOW - timedelta(minutes=16))
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "STALE" in r.order.reject_reason


def test_forming_candles_cannot_reach_the_archive(env):
    # Phase 2 archives only final bars (ck_candles_only_finalized_archived); the gate's own
    # NOT_FINAL rule is covered in tests/unit/test_price_gate.py for other sources.
    with pytest.raises(IntegrityError):
        c = make_candle(env["db"], env["provider"], env["inst"], "100")
        c.is_final = False
        env["db"].commit()
    env["db"].rollback()


def test_restated_observation_is_never_used_for_execution(env):
    # Phase 2 decision: the first observation stays canonical; later versions are non-canonical.
    make_candle(env["db"], env["provider"], env["inst"], "100")
    make_candle(env["db"], env["provider"], env["inst"], "105", version=2, observed_delay=timedelta(minutes=2))
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.FILLED
    fill = env["db"].scalar(select(Fill))
    assert fill.reference_price == D("100")


def test_disabled_instrument_is_not_tradable(env):
    from app.db.enums import TradingStatus

    off = make_instrument(env["db"], "OFF", trading_status=TradingStatus.DISABLED, disabled_reason="test: disabled")
    with pytest.raises(InvalidOrder, match="INSTRUMENT_DISABLED"):
        go(env, intent(off))


def test_observation_received_after_decision_time_is_not_used(env):
    # Bar ended before "now" but Agent-X only received it later: using it would be lookahead.
    make_candle(env["db"], env["provider"], env["inst"], "100", end=NOW - timedelta(seconds=10),
                observed_delay=timedelta(seconds=30))
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "NO_DATA" in r.order.reject_reason


def test_other_providers_data_is_never_substituted(env):
    other = make_provider(env["db"], "other")
    make_candle(env["db"], env["provider"], env["inst"], "100", end=NOW - timedelta(minutes=30))  # stale
    make_candle(env["db"], other, env["inst"], "101")  # fresh, but not the approved provider
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "STALE" in r.order.reject_reason


def test_ambiguous_provider_configuration_executes_nothing(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    s = enabled_settings(env["settings"], execution_approved_providers=["fixture", "other"])
    r = go(env, intent(env["inst"]), settings=s)
    assert r.order.status is OrderStatus.REJECTED and "NO_DATA" in r.order.reject_reason


def test_missing_cost_assumptions_block_execution(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"]), settings=enabled_settings(env["settings"], exchange_slippage_bps=None))
    assert r.order.status is OrderStatus.REJECTED and "COSTS_NOT_CONFIGURED" in r.order.reject_reason


# ---- happy path: fills, fees, slippage, ledger, positions -------------------------------------


def test_market_buy_then_sell_accounting_is_exact(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    buy = go(env, intent(env["inst"], qty=10, take_profit_price=D("120")))
    assert buy.order.status is OrderStatus.FILLED and buy.order.filled_quantity == 10
    assert buy.decision.outcome is RiskOutcome.APPROVED and buy.decision.risk_amount == D("50.5")
    fill = db.scalar(select(Fill).where(Fill.order_id == buy.order.id))
    assert (fill.reference_price, fill.price, fill.turnover, fill.fee) == (
        D("100"), D("100.05"), D("1000.5"), D("21.0005")
    )
    assert fill.provider_code == "fixture" and fill.price_basis is PriceBasis.AS_TRADED_VERIFIED
    assert fill.price_source_ref.startswith("candle:") and fill.executed_at == NOW
    assert cash(env) == D("98978.4995")
    pos = db.scalar(select(Position))
    assert (pos.quantity, pos.avg_entry_price, pos.stop_loss_price, pos.take_profit_price) == (
        10, D("100.05"), D("95"), D("120")
    )

    env["clock"].now = NOW + timedelta(minutes=5)
    make_candle(db, env["provider"], env["inst"], "110", end=NOW)
    sell = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10))
    assert sell.order.status is OrderStatus.FILLED
    sfill = db.scalar(select(Fill).where(Fill.order_id == sell.order.id))
    assert (sfill.price, sfill.turnover, sfill.fee) == (D("109.945"), D("1099.45"), D("23.29835"))
    db.expire_all()
    pos = db.scalar(select(Position))
    assert pos.quantity == 0 and pos.avg_entry_price is None and pos.stop_loss_price is None
    assert pos.realized_pnl == D("98.95") and pos.fees_paid == D("44.29885")
    assert cash(env) == D("100054.65115")
    # ledger reconciles: initial capital + entries = cash
    entries = db.scalars(select(CashLedgerEntry.amount)).all()
    assert D("100000") + sum(entries) == cash(env)


def test_unrealized_pnl_and_equity_use_only_eligible_marks(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    env["clock"].now = NOW + timedelta(minutes=5)
    make_candle(db, env["provider"], env["inst"], "102", end=NOW)
    v = ctx(env).portfolio.valuation(env["account"])
    assert v.positions[0].mark == D("102") and v.positions[0].unrealized_pnl == D("19.5")
    assert v.equity == D("98978.4995") + D("1020")

    env["clock"].now = NOW + timedelta(hours=1)  # marks are now stale
    v = ctx(env).portfolio.valuation(env["account"])
    assert v.equity is None and v.unmarked_instrument_ids == [env["inst"].id]
    assert v.positions[0].unrealized_pnl is None


def test_insufficient_cash(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    poor = make_account(db, capital="500.00")
    configure_risk(db, poor, resize_allowed=False)
    r = submit(db, env["settings"], env["clock"], poor, intent(env["inst"], qty=10))
    assert r.order.status is OrderStatus.REJECTED and "RISK_REJECTED" in r.order.reject_reason
    assert "available_cash" in r.order.reject_reason


def test_exchange_rejects_insufficient_cash_even_if_risk_were_bypassed(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    poor = make_account(db, capital="500.00")
    c = ctx(env)
    order = c.exchange.submit(poor, intent(env["inst"], qty=10), "fp", quantity=10)
    assert order.status is OrderStatus.REJECTED and order.reject_reason.startswith("INSUFFICIENT_CASH")


def test_short_selling_is_rejected_by_risk_and_by_the_exchange(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"], side=OrderSide.SELL, qty=1))
    assert r.order.status is OrderStatus.REJECTED and "INSUFFICIENT_POSITION" in r.order.reject_reason
    direct = ctx(env).exchange.submit(env["account"], intent(env["inst"], side=OrderSide.SELL, qty=1), "fp", 1)
    assert direct.status is OrderStatus.REJECTED and direct.reject_reason.startswith("INSUFFICIENT_POSITION")


# ---- validation -------------------------------------------------------------------------------


def test_invalid_instrument_and_quantity(env):
    with pytest.raises(InvalidOrder, match="UNKNOWN_INSTRUMENT"):
        go(env, intent(env["inst"], instrument_id=999_999))
    env["db"].rollback()
    with pytest.raises(InvalidOrder, match="INVALID_QUANTITY"):
        go(env, intent(env["inst"], qty=0))
    env["db"].rollback()
    inactive = make_instrument(env["db"], "DEAD", is_active=False)
    with pytest.raises(InvalidOrder, match="INSTRUMENT_INACTIVE"):
        go(env, intent(inactive))
    env["db"].rollback()
    lots = make_instrument(env["db"], "LOTS", lot_size=5)
    with pytest.raises(InvalidOrder, match="lot size"):
        go(env, intent(lots, qty=7))
    env["db"].rollback()
    assert count(env["db"], Order) == 0


def test_futures_are_not_tradable(env):
    from app.db.enums import InstrumentType

    from app.db.enums import TradingStatus

    # Phase 2 forces futures to be DISABLED; either guard rejects the order.
    fut = make_instrument(
        env["db"], "FUT", instrument_type=InstrumentType.FUTURE, trading_status=TradingStatus.DISABLED,
        disabled_reason="futures disabled",
    )
    with pytest.raises(InvalidOrder, match="INSTRUMENT_DISABLED|INSTRUMENT_TYPE_NOT_SUPPORTED"):
        go(env, intent(fut))


# ---- idempotency and duplicate prevention -----------------------------------------------------


def test_resubmitting_the_same_order_does_not_execute_twice(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    i = intent(env["inst"], qty=10)
    first = go(env, i)
    again = go(env, i)
    assert again.replayed and again.order.id == first.order.id
    assert count(env["db"], Order) == 1 and count(env["db"], Fill) == 1
    assert count(env["db"], RiskDecision) == 1


def test_concurrent_duplicate_submissions_execute_once(env, engine):
    """Two connections race the same order; the account row lock serializes them."""
    import threading

    from sqlalchemy.orm import Session

    make_candle(env["db"], env["provider"], env["inst"], "100")
    i = intent(env["inst"], qty=10)
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def worker():
        with Session(engine) as s:
            account = s.merge(env["account"])
            barrier.wait()
            try:
                outcomes.append(submit(s, env["settings"], env["clock"], account, i).replayed)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert sorted(outcomes) == [False, True]
    assert count(env["db"], Order) == 1 and count(env["db"], Fill) == 1


def test_reusing_a_client_order_id_for_a_different_order_is_a_conflict(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    i = intent(env["inst"], qty=10)
    go(env, i)
    from dataclasses import replace

    with pytest.raises(IdempotencyConflict):
        go(env, replace(i, quantity=11))


def test_same_observation_never_fills_an_order_twice(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100", volume=1000)
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))  # 10 shares/observation
    r = go(env, intent(env["inst"], qty=25, otype=OrderType.LIMIT, limit_price=D("101")), settings=s)
    assert r.order.status is OrderStatus.PARTIALLY_FILLED and r.order.filled_quantity == 10
    TradingContext.build(db, s, env["clock"]).exchange.process_open_orders(env["account"])
    db.commit()
    db.refresh(r.order)
    assert r.order.filled_quantity == 10 and count(db, Fill) == 1


def test_database_rejects_a_duplicate_fill_for_the_same_observation(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    f = db.scalar(select(Fill))
    dup = Fill(**{c.key: getattr(f, c.key) for c in Fill.__table__.columns if c.key not in ("id", "seq")}, seq=2)
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---- partial fills, limit orders, cancellation ------------------------------------------------


def test_limit_order_rests_then_fills_partially_across_observations(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "102", volume=1000)
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    r = go(env, intent(env["inst"], qty=25, otype=OrderType.LIMIT, limit_price=D("101")), settings=s)
    assert r.order.status is OrderStatus.PENDING and r.order.filled_quantity == 0
    reserved = r.order.reserved_cash
    assert reserved == D("2525") + D("20") + D("2.525")  # 25 * 101 + fixed + 10 bps

    for minute, price, expected_filled, expected_status in [
        (5, "100.80", 10, OrderStatus.PARTIALLY_FILLED),
        (10, "103", 10, OrderStatus.PARTIALLY_FILLED),  # not marketable
        (15, "100", 20, OrderStatus.PARTIALLY_FILLED),
        (20, "99", 25, OrderStatus.FILLED),
    ]:
        env["clock"].now = NOW + timedelta(minutes=minute)
        make_candle(db, env["provider"], env["inst"], price, end=env["clock"].now - timedelta(minutes=1), volume=1000)
        TradingContext.build(db, s, env["clock"]).exchange.process_open_orders(env["account"])
        db.commit()
        db.refresh(r.order)
        assert (r.order.filled_quantity, r.order.status) == (expected_filled, expected_status)

    fills = db.scalars(select(Fill).order_by(Fill.seq)).all()
    assert [f.seq for f in fills] == [1, 2, 3]
    assert all(f.price <= D("101") for f in fills)  # never above the limit
    assert fills[0].fee > D("20") and fills[1].fee < D("20")  # fixed fee only on the first fill
    assert r.order.reserved_cash == 0


def test_market_order_remainder_is_cancelled_ioc(env):
    make_candle(env["db"], env["provider"], env["inst"], "100", volume=1000)
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    r = go(env, intent(env["inst"], qty=25), settings=s)
    assert r.order.status is OrderStatus.CANCELLED and r.order.filled_quantity == 10
    assert "IOC" in r.order.cancel_reason and r.order.reserved_cash == 0


def test_cancel_releases_reservation_and_terminal_orders_cannot_be_cancelled(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    assert r.order.status is OrderStatus.PENDING and r.order.reserved_cash > 0
    assert ctx(env).portfolio.available_cash(env["account"]) == D("100000") - r.order.reserved_cash
    c = ctx(env)
    c.exchange.cancel(env["account"], r.order.id, "user request")
    db.commit()
    assert r.order.status is OrderStatus.CANCELLED and r.order.reserved_cash == 0
    assert ctx(env).portfolio.available_cash(env["account"]) == D("100000")
    with pytest.raises(OrderNotCancellable):
        ctx(env).exchange.cancel(env["account"], r.order.id, "again")
    db.rollback()


def test_pending_sell_quantity_cannot_be_sold_twice(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    resting = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("150")))
    assert resting.order.status is OrderStatus.PENDING
    r = go(env, intent(env["inst"], side=OrderSide.SELL, qty=1))
    assert r.order.status is OrderStatus.REJECTED and "INSUFFICIENT_POSITION" in r.order.reject_reason


# ---- risk integration -------------------------------------------------------------------------


def test_risk_settings_are_required_for_entries(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    fresh = make_account(db)
    r = submit(db, env["settings"], env["clock"], fresh, intent(env["inst"]))
    assert "RISK_SETTINGS_NOT_CONFIGURED" in r.order.reject_reason


def test_oversized_order_is_resized_before_reaching_the_exchange(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"], qty=500))
    assert r.decision.outcome is RiskOutcome.RESIZED
    assert r.order.requested_quantity == 500 and r.order.quantity == 198 and r.order.filled_quantity == 198
    assert r.decision.user_limits["max_risk_per_trade_pct"] == "1.0000"
    assert any(c["name"] == "max_risk_per_trade" and not c["passed"] for c in r.decision.checks)


def test_decision_log_records_strategy_constraints(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"], qty=500, strategy_id="S1", strategy_max_risk_pct=D("0.5")))
    d = r.decision
    assert d.strategy_id == "S1" and d.approved_quantity == 99
    assert d.strategy_constraints == {"strategy_id": "S1", "strategy_max_risk_pct": "0.5", "effective_risk_pct": "0.5"}
    assert d.price_check["quote"]["provider_code"] == "fixture"


def test_drawdown_and_daily_loss_limits_block_entries(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    state = RiskState(user_id=env["account"].user_id, peak_equity=D("200000"))
    db.add(state)
    db.commit()
    r = go(env, intent(env["inst"]))
    assert "MAX_DRAWDOWN_BREACHED" in r.order.reject_reason

    state.peak_equity = D("100000")
    state.day_session_date = NOW.date()
    state.day_start_equity = D("110000")
    db.commit()
    r = go(env, intent(env["inst"]))
    assert "DAILY_LOSS_LIMIT_BREACHED" in r.order.reject_reason


def test_kill_switch_cancels_open_orders_blocks_entries_but_allows_exits(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    resting = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("90"), stop_loss_price=D("85")))
    assert resting.order.status is OrderStatus.PENDING

    c = ctx(env)
    c.risk.set_kill_switch(env["account"], True, "manual emergency", c.exchange)
    db.commit()
    db.refresh(resting.order)
    assert resting.order.status is OrderStatus.CANCELLED and "KILL_SWITCH" in resting.order.cancel_reason

    blocked = go(env, intent(env["inst"], qty=1))
    assert "KILL_SWITCH_ENGAGED" in blocked.order.reject_reason
    direct = ctx(env).exchange.submit(env["account"], intent(env["inst"], qty=1), "fp", 1)
    assert direct.reject_reason == "KILL_SWITCH_ENGAGED"  # the exchange checks independently

    exit_ = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10))
    assert exit_.order.status is OrderStatus.FILLED
    events = db.scalars(select(AuditEvent.event_type)).all()
    assert "risk.kill_switch_engaged" in events


def test_protective_stop_loss_exit(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))  # stop 95
    env["clock"].now = NOW + timedelta(minutes=5)
    make_candle(db, env["provider"], env["inst"], "94", end=NOW)
    service = OrderService(ctx(env))
    results = service.run_protective_exits(env["account"].user_id)
    db.commit()
    assert len(results) == 1 and results[0].order.status is OrderStatus.FILLED
    assert results[0].order.source.value == "RISK_EXIT"
    # same observation again: idempotent, no second exit
    again = OrderService(ctx(env)).run_protective_exits(env["account"].user_id)
    assert again == []
    assert db.scalar(select(Position.quantity)) == 0


# ---- transactional integrity ------------------------------------------------------------------


def test_failure_during_accounting_rolls_back_everything(env, monkeypatch):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    from app.exchange import service as exchange_service

    def boom(*a, **k):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(exchange_service.MockExchange, "_ledger", boom)
    with pytest.raises(RuntimeError):
        OrderService(ctx(env)).submit(env["account"].user_id, intent(env["inst"], qty=10))
    db.rollback()
    for model in (Order, Fill, CashLedgerEntry, Position, RiskDecision):
        assert count(db, model) == 0, model.__name__


# ---- database-level guarantees ----------------------------------------------------------------


def _expect_db_rejection(db, sql, **params):
    with pytest.raises((IntegrityError, DBAPIError)):
        db.execute(text(sql), params)
        db.commit()
    db.rollback()


def test_fills_ledger_and_decisions_are_append_only(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    for table in ("fills", "cash_ledger", "risk_decisions"):
        _expect_db_rejection(env["db"], f"update {table} set user_id = user_id")
        _expect_db_rejection(env["db"], f"delete from {table}")


def test_terminal_orders_are_immutable_and_orders_cannot_be_deleted(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    r = go(env, intent(env["inst"], qty=10))
    _expect_db_rejection(env["db"], "update orders set status = 'CANCELLED', cancel_reason = 'x' where id = :i", i=r.order.id)
    _expect_db_rejection(env["db"], "delete from orders")


def test_order_identity_is_immutable_and_fills_cannot_shrink(env):
    make_candle(env["db"], env["provider"], env["inst"], "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    _expect_db_rejection(env["db"], "update orders set quantity = 4 where id = :i", i=r.order.id)
    _expect_db_rejection(env["db"], "update orders set side = 'SELL' where id = :i", i=r.order.id)


def test_database_refuses_a_fill_priced_from_unverified_data(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    _expect_db_rejection(
        env["db"],
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, price_as_of, price_observed_at,"
        " cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity, reference_price,"
        " price, turnover, fee, price_source_kind, 'x', provider_code, 'UNVERIFIED_PROVIDER_NATIVE', price_as_of,"
        " price_observed_at, cost_model, executed_at from fills limit 1",
    )


def test_database_refuses_a_fill_using_a_future_observation(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    _expect_db_rejection(
        env["db"],
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, price_as_of, price_observed_at,"
        " cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity, reference_price,"
        " price, turnover, fee, price_source_kind, 'y', provider_code, price_basis,"
        " executed_at + interval '1 minute', price_observed_at, cost_model, executed_at from fills limit 1",
    )


def test_downgrading_0004_is_refused_while_trading_records_exist(env, engine):
    from alembic import command

    from tests.conftest import alembic_config

    make_candle(env["db"], env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    with pytest.raises(RuntimeError, match="cannot downgrade 0004"):
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0003")
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0004"
        assert conn.execute(text("select count(*) from fills")).scalar() == 1


def test_position_cannot_go_short_at_the_database_level(env):
    make_candle(env["db"], env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    _expect_db_rejection(env["db"], "update positions set quantity = -1")


def test_users_cannot_see_each_others_trading_records(env):
    db = env["db"]
    make_candle(db, env["provider"], env["inst"], "100")
    go(env, intent(env["inst"], qty=10))
    other = make_account(db)
    c = ctx(env)
    assert c.portfolio.open_positions(other.user_id) == []
    assert c.portfolio.cash_balance(other) == D("100000")
    order_id = db.scalar(select(Order.id))
    with pytest.raises(OrderNotFound):
        c.exchange.cancel(other, order_id, "not mine")
