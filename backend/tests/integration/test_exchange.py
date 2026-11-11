"""Mock Exchange + risk pipeline against a real PostgreSQL, with deterministic synthetic fixtures.

Execution model under test: orders never fill at submission; they fill at the open of the first finalized
bar that starts at or after acceptance (see app/exchange/service.py).
"""

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
    next_bar,
    process,
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
        db=db, provider=provider, inst=inst, account=account, settings=enabled_settings(test_settings), clock=Clock()
    )


def go(env, order_intent, settings=None, account=None):
    return submit(env["db"], settings or env["settings"], env["clock"], account or env["account"], order_intent)


def ctx(env, settings=None):
    return TradingContext.build(env["db"], settings or env["settings"], env["clock"])


def bar(env, open_, close=None, **kw):
    return next_bar(env["db"], env["provider"], env["inst"], env["clock"], open_, close, **kw)


def run(env, settings=None, account=None):
    return process(env["db"], settings or env["settings"], env["clock"], account or env["account"])


def reference(env, close="100"):
    """The finalized bar a decision is based on (ended 5 minutes before NOW)."""
    return make_candle(env["db"], env["provider"], env["inst"], close)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def cash(env, account=None) -> Decimal:
    return ctx(env).portfolio.cash_balance(account or env["account"])


def reload(env, order):
    env["db"].refresh(order)
    return order


# ---- decision D4: default configuration and price eligibility ----------------------------------------


def test_default_configuration_blocks_every_fill(env, test_settings):
    reference(env)
    r = go(env, intent(env["inst"]), settings=test_settings)  # untouched defaults
    assert r.order.status is OrderStatus.REJECTED
    assert "NO_ELIGIBLE_PRICE" in r.order.reject_reason and "SOURCE_DISABLED" in r.order.reject_reason
    assert r.decision.outcome is RiskOutcome.REJECTED
    assert count(env["db"], Fill) == 0 and count(env["db"], CashLedgerEntry) == 0


def test_unverified_provider_native_data_is_never_used(env):
    make_candle(env["db"], env["provider"], env["inst"], "100", verified=False)
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "UNVERIFIED_BASIS" in r.order.reject_reason


def test_an_unverified_next_bar_blocks_the_fill(env):
    reference(env)
    r = go(env, intent(env["inst"]))
    make_candle(env["db"], env["provider"], env["inst"], "100", end=NOW + timedelta(minutes=5), verified=False)
    env["clock"].now = NOW + timedelta(minutes=6)
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and count(env["db"], Fill) == 0
    assert r.order.cancel_reason.startswith("FILL_BAR_INELIGIBLE: UNVERIFIED_BASIS")


def test_stale_observation_blocks_new_orders(env):
    make_candle(env["db"], env["provider"], env["inst"], "100", end=NOW - timedelta(minutes=16))
    r = go(env, intent(env["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "STALE" in r.order.reject_reason


def test_forming_candles_cannot_reach_the_archive(env):
    # Phase 2 archives only final bars (ck_candles_only_finalized_archived); the gate's own NOT_FINAL rule
    # is covered in tests/unit/test_price_gate.py.
    with pytest.raises(IntegrityError):
        c = make_candle(env["db"], env["provider"], env["inst"], "100")
        c.is_final = False
        env["db"].commit()
    env["db"].rollback()


def test_observation_received_after_decision_time_is_not_used(env):
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
    reference(env)
    s = enabled_settings(env["settings"], execution_approved_providers=["fixture", "other"])
    r = go(env, intent(env["inst"]), settings=s)
    assert r.order.status is OrderStatus.REJECTED and "CONFIG_INVALID" in r.order.reject_reason
    assert "exactly one provider" in r.order.reject_reason


def test_missing_cost_assumptions_block_execution(env):
    reference(env)
    r = go(env, intent(env["inst"]), settings=enabled_settings(env["settings"], exchange_cost_schedule_path=None))
    assert r.order.status is OrderStatus.REJECTED and "CONFIG_INVALID" in r.order.reject_reason
    assert "EXCHANGE_COST_SCHEDULE_PATH" in r.order.reject_reason


def test_restated_bars_are_never_used(env):
    reference(env)
    make_candle(env["db"], env["provider"], env["inst"], "130", version=2, observed_delay=timedelta(minutes=2))
    r = go(env, intent(env["inst"]))
    assert r.decision.sizing_price == D("100.05")  # decision used the canonical reference, not the restatement
    bar(env, "100")
    make_candle(env["db"], env["provider"], env["inst"], "120", end=NOW + timedelta(minutes=5), open="120",
                version=2, observed_delay=timedelta(seconds=45))
    run(env)
    assert env["db"].scalar(select(Fill.reference_price)) == D("100")


def test_disabled_instrument_is_not_tradable(env):
    from app.db.enums import TradingStatus

    off = make_instrument(env["db"], "OFF", trading_status=TradingStatus.DISABLED, disabled_reason="test: disabled")
    with pytest.raises(InvalidOrder, match="INSTRUMENT_DISABLED"):
        go(env, intent(off))


# ---- next-bar-open execution model ----------------------------------------------------------------------


def test_orders_never_fill_at_submission(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=10))
    assert r.order.status is OrderStatus.PENDING and r.order.filled_quantity == 0
    assert r.order.accepted_at == NOW and r.order.risk_budget == D("1000")  # 1 % of 100000
    exact = D("1000.5") + D("1.834917")
    # reservation = worst case: exact cost plus at most one rounding quantum per component (GST included)
    assert exact <= r.order.reserved_cash <= exact + D("0.00002")
    assert count(env["db"], Fill) == 0


def test_market_order_fills_at_the_next_bar_open_not_the_decision_close(env):
    reference(env, "100")
    r = go(env, intent(env["inst"], qty=10))
    bar(env, "101", "104")
    run(env)
    fill = env["db"].scalar(select(Fill))
    assert fill.reference_price == D("101") and fill.price == D("101.0505")  # next open + 5 bps slippage
    assert fill.price_source_kind == "NEXT_BAR_OPEN"
    assert fill.price_as_of == NOW  # the open is known at the start of the next bar
    assert reload(env, r.order).status is OrderStatus.FILLED


def test_buy_then_sell_accounting_is_exact_with_component_fees(env):
    db = env["db"]
    reference(env)
    buy = go(env, intent(env["inst"], qty=10, take_profit_price=D("120")))
    bar(env, "100")
    run(env)
    fill = db.scalar(select(Fill).where(Fill.order_id == buy.order.id))
    assert (fill.price, fill.turnover, fill.fee) == (D("100.05"), D("1000.5"), D("1.834917"))
    comps = {k: D(v["charged"]) for k, v in fill.fee_breakdown["components"].items()}
    assert comps == {
        "BROKERAGE": D("1.0005"), "STT": D("0"), "TXN_CHARGE": D("0.30015"), "SEBI_FEE": D("0"), "IPFT": D("0"),
        "STAMP_DUTY": D("0.30015"), "GST": D("0.234117"),
    }
    assert D(fill.fee_breakdown["total"]) == D("1.834917")
    assert fill.fee_breakdown["components"]["BROKERAGE"]["rate_id"] == "T-BRK-D"
    assert fill.cost_model["schedule_version"] == "SYNTHETIC-TEST-1" and len(fill.cost_model["schedule_sha256"]) == 64
    assert fill.realized_pnl == 0
    assert fill.provider_code == "fixture" and fill.price_basis is PriceBasis.AS_TRADED_VERIFIED
    assert cash(env) == D("98997.665083")
    pos = db.scalar(select(Position))
    assert (pos.quantity, pos.avg_entry_price, pos.stop_loss_price, pos.take_profit_price) == (
        10, D("100.05"), D("95"), D("120")
    )

    sell = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10))
    assert sell.order.status is OrderStatus.PENDING
    bar(env, "110")
    run(env)
    sfill = db.scalar(select(Fill).where(Fill.order_id == sell.order.id))
    assert (sfill.price, sfill.turnover, sfill.fee) == (D("109.945"), D("1099.45"), D("4.435182"))
    sc = sfill.fee_breakdown["components"]
    assert D(sc["STT"]["charged"]) == D("2.748625") and "STAMP_DUTY" not in sc and D(sc["DP_CHARGE"]["charged"]) == 0
    assert sfill.realized_pnl == D("98.95")
    db.expire_all()
    pos = db.scalar(select(Position))
    assert pos.quantity == 0 and pos.avg_entry_price is None and pos.stop_loss_price is None
    assert pos.realized_pnl == D("98.95") and pos.fees_paid == D("6.270099")
    assert cash(env) == D("100092.679901")
    assert D("100000") + sum(db.scalars(select(CashLedgerEntry.amount)).all()) == cash(env)


def test_market_order_is_cancelled_when_the_next_bar_is_missing(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=10))
    env["clock"].now = NOW + timedelta(minutes=10)  # the 05:00 bar never arrived (data gap)
    bar(env, "100")  # 05:10 bar
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("DATA_GAP")
    assert r.order.reserved_cash == 0 and count(env["db"], Fill) == 0


def test_market_order_is_not_filled_across_the_overnight_gap(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=10))
    env["clock"].now = NOW + timedelta(days=1)  # next session, no bar for the order's session
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and "NEXT_BAR_UNAVAILABLE" in r.order.cancel_reason


def test_entry_is_cancelled_when_the_next_open_gaps_through_the_stop(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=10))  # stop 95
    bar(env, "94")
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("GAP_THROUGH_STOP")
    assert count(env["db"], Fill) == 0


def test_gap_up_fill_is_capped_by_the_approved_risk_budget(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=500))  # resized to 198 (1 % hard limit = 1000)
    assert r.order.quantity == 198 and r.order.risk_budget == D("1000")
    bar(env, "102")  # fill at 102.051: per-share risk 7.051
    run(env)
    reload(env, r.order)
    assert r.order.filled_quantity == 141  # floor(1000 / 7.051)
    assert r.order.status is OrderStatus.CANCELLED and "RISK_BUDGET" in r.order.cancel_reason
    realized_risk = (r.order.avg_fill_price - r.order.stop_loss_price) * r.order.filled_quantity
    assert realized_risk <= r.order.risk_budget


def test_exchange_caps_a_gap_up_fill_by_available_cash(env):
    reference(env)
    small = make_account(env["db"], capital="1050.00")
    order = ctx(env).exchange.submit(small, intent(env["inst"], qty=10), "fp", 10)  # bypasses risk: no budget
    env["db"].commit()
    assert order.status is OrderStatus.PENDING
    bar(env, "108")  # 10 x 108.054 > 1050
    run(env, account=small)
    reload(env, order)
    assert order.filled_quantity == 9 and "INSUFFICIENT_CASH_AT_FILL" in order.cancel_reason
    assert cash(env, small) >= 0


def test_limit_buy_fills_at_open_below_limit_or_at_limit_when_crossed(env):
    reference(env, "102")
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    r = go(env, intent(env["inst"], qty=25, otype=OrderType.LIMIT, limit_price=D("101")), settings=s)
    assert r.order.status is OrderStatus.PENDING
    assert D("2525") + COSTS_FEE_2525 <= r.order.reserved_cash <= D("2525") + COSTS_FEE_2525 + D("0.00002")

    for open_, low, expected_filled, expected_kind in [
        ("103", "101", 0, None),  # only touches the limit (low == limit): not a fill
        ("102", "100.8", 10, "BAR_LIMIT_CROSS"),  # crossed inside the bar: filled at the limit
        ("100", "99.5", 20, "NEXT_BAR_OPEN"),  # opens below the limit: filled at min(slipped open, limit)
        ("99", "98", 25, "NEXT_BAR_OPEN"),
    ]:
        bar(env, open_, low=low, volume=1000)
        process(env["db"], s, env["clock"], env["account"])
        assert reload(env, r.order).filled_quantity == expected_filled
    fills = env["db"].scalars(select(Fill).order_by(Fill.seq)).all()
    assert [f.price_source_kind for f in fills] == ["BAR_LIMIT_CROSS", "NEXT_BAR_OPEN", "NEXT_BAR_OPEN"]
    assert [f.price for f in fills] == [D("101"), D("100.05"), D("99.0495")]
    assert all(f.price <= D("101") for f in fills)
    assert r.order.status is OrderStatus.FILLED and r.order.reserved_cash == 0
    # brokerage cap is per order across partial fills
    assert sum(D(f.fee_breakdown["components"]["BROKERAGE"]["charged"]) for f in fills) <= D("20")


COSTS_FEE_2525 = D("4.63085")  # fees on 2525: brokerage 2.525 + txn .7575 + stamp .7575 + gst .59085


def test_limit_sell_fills_at_open_above_limit_or_at_limit_when_crossed(env):
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    r = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("105")))
    bar(env, "101", high="104.9")  # no fill
    run(env)
    assert reload(env, r.order).filled_quantity == 0
    bar(env, "103", high="106")  # crossed: filled at the limit
    run(env)
    f = env["db"].scalar(select(Fill).where(Fill.order_id == r.order.id))
    assert f.price == D("105") and f.price_source_kind == "BAR_LIMIT_CROSS"


def test_limit_order_expires_at_the_end_of_its_session(env):
    reference(env, "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100"), stop_loss_price=D("90")))
    env["clock"].now = NOW + timedelta(days=1)
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("DAY_ORDER_EXPIRED")
    assert r.order.reserved_cash == 0


def test_a_resting_limit_never_fills_on_the_next_session(env):
    reference(env, "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100"), stop_loss_price=D("90")))
    env["clock"].now = NOW + timedelta(days=1)
    bar(env, "98")  # next session opens below the limit
    run(env)
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("DAY_ORDER_EXPIRED")
    assert count(env["db"], Fill) == 0


def test_brokerage_cap_applies_once_per_order_across_partial_fills(env):
    reference(env, "102")
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.5"))
    c = TradingContext.build(env["db"], s, env["clock"])
    order = c.exchange.submit(  # direct: large size, bypassing risk sizing
        env["account"], intent(env["inst"], qty=900, otype=OrderType.LIMIT, limit_price=D("101")), "fp", 900
    )
    env["db"].commit()
    bar(env, "100", volume=1000)  # 500 shares, turnover ~50k: 10 bps = ~50 -> capped at 20
    process(env["db"], s, env["clock"], env["account"])
    bar(env, "100", volume=1000)  # remaining 400: brokerage cap already used up
    process(env["db"], s, env["clock"], env["account"])
    fills = env["db"].scalars(select(Fill).order_by(Fill.seq)).all()
    assert [f.quantity for f in fills] == [500, 400] and reload(env, order).status is OrderStatus.FILLED
    assert [D(f.fee_breakdown["components"]["BROKERAGE"]["charged"]) for f in fills] == [D("20"), D("0")]


def test_market_remainder_is_cancelled_after_the_next_bar(env):
    reference(env)
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    r = go(env, intent(env["inst"], qty=25), settings=s)
    bar(env, "100", volume=1000)
    process(env["db"], s, env["clock"], env["account"])
    reload(env, r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.filled_quantity == 10 and "IOC" in r.order.cancel_reason


def test_the_same_bar_never_fills_an_order_twice(env):
    reference(env, "102")
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    r = go(env, intent(env["inst"], qty=25, otype=OrderType.LIMIT, limit_price=D("101")), settings=s)
    bar(env, "100", volume=1000)
    for _ in range(3):
        process(env["db"], s, env["clock"], env["account"])
    assert reload(env, r.order).filled_quantity == 10 and count(env["db"], Fill) == 1


def test_database_rejects_a_duplicate_fill_for_the_same_bar(env):
    db = env["db"]
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    f = db.scalar(select(Fill))
    dup = Fill(**{c.key: getattr(f, c.key) for c in Fill.__table__.columns if c.key not in ("id", "seq")}, seq=2)
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---- validation, cash and position --------------------------------------------------------------------


def test_insufficient_cash_is_rejected_by_risk(env):
    reference(env)
    poor = make_account(env["db"], capital="500.00")
    configure_risk(env["db"], poor, resize_allowed=False)
    r = go(env, intent(env["inst"], qty=10), account=poor)
    assert r.order.status is OrderStatus.REJECTED and "available_cash" in r.order.reject_reason


def test_exchange_rejects_insufficient_cash_even_if_risk_were_bypassed(env):
    reference(env)
    poor = make_account(env["db"], capital="500.00")
    order = ctx(env).exchange.submit(poor, intent(env["inst"], qty=10), "fp", quantity=10)
    assert order.status is OrderStatus.REJECTED and order.reject_reason.startswith("INSUFFICIENT_CASH")


def test_exchange_requires_a_stop_loss_on_entries_independently(env):
    reference(env)
    order = ctx(env).exchange.submit(env["account"], intent(env["inst"], stop_loss_price=None), "fp", 10)
    assert order.status is OrderStatus.REJECTED and order.reject_reason.startswith("STOP_LOSS_REQUIRED")


def test_short_selling_is_rejected_by_risk_and_by_the_exchange(env):
    reference(env)
    r = go(env, intent(env["inst"], side=OrderSide.SELL, qty=1))
    assert r.order.status is OrderStatus.REJECTED and "INSUFFICIENT_POSITION" in r.order.reject_reason
    direct = ctx(env).exchange.submit(env["account"], intent(env["inst"], side=OrderSide.SELL, qty=1), "fp", 1)
    assert direct.status is OrderStatus.REJECTED and direct.reject_reason.startswith("INSUFFICIENT_POSITION")


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
    from app.db.enums import InstrumentType, TradingStatus

    fut = make_instrument(env["db"], "FUT", instrument_type=InstrumentType.FUTURE, trading_status=TradingStatus.DISABLED,
                          disabled_reason="futures disabled")
    with pytest.raises(InvalidOrder, match="INSTRUMENT_DISABLED|INSTRUMENT_TYPE_NOT_SUPPORTED"):
        go(env, intent(fut))


def test_pending_sell_quantity_cannot_be_sold_twice(env):
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    resting = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("150")))
    assert resting.order.status is OrderStatus.PENDING
    r = go(env, intent(env["inst"], side=OrderSide.SELL, qty=1))
    assert r.order.status is OrderStatus.REJECTED and "INSUFFICIENT_POSITION" in r.order.reject_reason


def test_cancel_releases_reservation_and_terminal_orders_cannot_be_cancelled(env):
    reference(env, "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    assert ctx(env).portfolio.available_cash(env["account"]) == D("100000") - r.order.reserved_cash
    ctx(env).exchange.cancel(env["account"], r.order.id, "user request")
    env["db"].commit()
    assert r.order.status is OrderStatus.CANCELLED and r.order.reserved_cash == 0
    assert ctx(env).portfolio.available_cash(env["account"]) == D("100000")
    with pytest.raises(OrderNotCancellable):
        ctx(env).exchange.cancel(env["account"], r.order.id, "again")
    env["db"].rollback()


# ---- idempotency and concurrency ----------------------------------------------------------------------


def test_resubmitting_the_same_order_does_not_create_a_second_one(env):
    reference(env)
    i = intent(env["inst"], qty=10)
    first = go(env, i)
    again = go(env, i)
    assert again.replayed and again.order.id == first.order.id
    assert count(env["db"], Order) == 1 and count(env["db"], RiskDecision) == 1


def test_reusing_a_client_order_id_for_a_different_order_is_a_conflict(env):
    from dataclasses import replace

    reference(env)
    i = intent(env["inst"], qty=10)
    go(env, i)
    with pytest.raises(IdempotencyConflict):
        go(env, replace(i, quantity=11))


def test_concurrent_duplicate_submissions_create_one_order(env, engine):
    import threading

    from sqlalchemy.orm import Session

    reference(env)
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
    assert count(env["db"], Order) == 1


# ---- risk integration ---------------------------------------------------------------------------------


def test_risk_settings_are_required_for_entries(env):
    reference(env)
    fresh = make_account(env["db"])
    r = go(env, intent(env["inst"]), account=fresh)
    assert "RISK_SETTINGS_NOT_CONFIGURED" in r.order.reject_reason


def test_oversized_order_is_resized_before_reaching_the_exchange(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=500))
    assert r.decision.outcome is RiskOutcome.RESIZED
    assert r.order.requested_quantity == 500 and r.order.quantity == 198
    assert r.decision.user_limits["max_risk_per_trade_pct"] == "1.0000"
    assert any(c["name"] == "max_risk_per_trade" and not c["passed"] for c in r.decision.checks)


def test_decision_log_records_strategy_constraints(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=500, strategy_id="S1", strategy_max_risk_pct=D("0.5")))
    d = r.decision
    assert d.strategy_id == "S1" and d.approved_quantity == 99
    assert d.strategy_constraints == {"strategy_id": "S1", "strategy_max_risk_pct": "0.5", "effective_risk_pct": "0.5"}
    assert d.price_check["quote"]["provider_code"] == "fixture"


def test_drawdown_and_daily_loss_limits_block_entries(env):
    db = env["db"]
    reference(env)
    state = RiskState(user_id=env["account"].user_id, peak_equity=D("200000"))
    db.add(state)
    db.commit()
    assert "MAX_DRAWDOWN_BREACHED" in go(env, intent(env["inst"])).order.reject_reason
    state.peak_equity = D("100000")
    state.day_session_date = NOW.date()
    state.day_start_equity = D("110000")
    db.commit()
    assert "DAILY_LOSS_LIMIT_BREACHED" in go(env, intent(env["inst"])).order.reject_reason


def test_risk_refresh_updates_references_and_reports_breaches_without_an_order(env):
    db = env["db"]
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    c = ctx(env)
    status = c.risk.refresh(env["account"])
    db.commit()
    assert status.state.last_equity_at == env["clock"].now and status.state.last_equity == status.equity
    assert status.breaches == []
    # the price falls; no order is submitted, yet the refresh sees the breach
    env["clock"].now += timedelta(minutes=4)
    make_candle(db, env["provider"], env["inst"], "1", end=env["clock"].now - timedelta(minutes=1), open="1")
    status = ctx(env).risk.refresh(env["account"])
    db.commit()
    assert status.equity < D("99100")
    # equity fell by ~ 990 of ~ 99998 (< 1 %): within the 5 % daily / 10 % drawdown limits
    assert status.breaches == []
    # tighten the limits; a refresh alone (still no order) now reports both breaches
    db.execute(text("update risk_settings set max_daily_loss_pct = 0.5, max_drawdown_pct = 0.5"))
    db.commit()
    status = ctx(env).risk.refresh(env["account"])
    assert any(b.startswith("DAILY_LOSS_LIMIT_BREACHED") for b in status.breaches)
    assert any(b.startswith("MAX_DRAWDOWN_BREACHED") for b in status.breaches)


def test_kill_switch_cancels_open_orders_blocks_entries_but_allows_exits(env):
    db = env["db"]
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    resting = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("90"), stop_loss_price=D("85")))
    assert resting.order.status is OrderStatus.PENDING

    c = ctx(env)
    c.risk.set_kill_switch(env["account"], True, "manual emergency", c.exchange)
    db.commit()
    assert reload(env, resting.order).status is OrderStatus.CANCELLED and "KILL_SWITCH" in resting.order.cancel_reason

    assert "KILL_SWITCH_ENGAGED" in go(env, intent(env["inst"], qty=1)).order.reject_reason
    direct = ctx(env).exchange.submit(env["account"], intent(env["inst"], qty=1), "fp", 1)
    assert direct.reject_reason == "KILL_SWITCH_ENGAGED"  # the exchange checks independently

    exit_ = go(env, intent(env["inst"], side=OrderSide.SELL, qty=10))
    assert exit_.order.status is OrderStatus.PENDING
    bar(env, "100")
    run(env)
    assert reload(env, exit_.order).status is OrderStatus.FILLED
    assert "risk.kill_switch_engaged" in db.scalars(select(AuditEvent.event_type)).all()


@pytest.mark.parametrize("trigger_bar,expected_source", [(dict(low="94"), "SL"), (dict(high="121"), "TP")])
def test_protective_exit_triggers_on_the_bar_range_and_fills_at_the_next_open(env, trigger_bar, expected_source):
    db = env["db"]
    reference(env)
    go(env, intent(env["inst"], qty=10, take_profit_price=D("120")))  # stop 95, target 120
    bar(env, "100")
    run(env)
    bar(env, "100", **trigger_bar)  # traded through a level; the close stays at 100
    results = OrderService(ctx(env)).run_protective_exits(env["account"].user_id)
    db.commit()
    assert len(results) == 1 and results[0].order.status is OrderStatus.PENDING
    assert results[0].order.source.value == "RISK_EXIT"
    assert results[0].order.client_order_id.startswith(expected_source)
    assert OrderService(ctx(env)).run_protective_exits(env["account"].user_id) == []  # nothing left to sell
    bar(env, "92")  # gap: the exit fills at the next open, not at the stop
    run(env)
    exit_fill = db.scalar(select(Fill).where(Fill.order_id == results[0].order.id))
    assert exit_fill.reference_price == D("92")
    assert db.scalar(select(Position.quantity)) == 0


# ---- transactional integrity ---------------------------------------------------------------------------


def test_failure_during_accounting_rolls_back_everything(env, monkeypatch):
    db = env["db"]
    reference(env)
    go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    from app.exchange import service as exchange_service

    def boom(*a, **k):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(exchange_service.MockExchange, "_ledger", boom)
    with pytest.raises(RuntimeError):
        ctx(env).exchange.process_open_orders(env["account"])
    db.rollback()
    for model in (Fill, CashLedgerEntry, Position):
        assert count(db, model) == 0, model.__name__
    assert db.scalar(select(Order.status)) is OrderStatus.PENDING


# ---- database-level guarantees ------------------------------------------------------------------------


def _expect_db_rejection(db, sql, **params):
    with pytest.raises((IntegrityError, DBAPIError)):
        db.execute(text(sql), params)
        db.commit()
    db.rollback()


def _filled(env):
    reference(env)
    r = go(env, intent(env["inst"], qty=10))
    bar(env, "100")
    run(env)
    return r


def test_fills_ledger_and_decisions_are_append_only(env):
    _filled(env)
    for table in ("fills", "cash_ledger", "risk_decisions"):
        _expect_db_rejection(env["db"], f"update {table} set user_id = user_id")
        _expect_db_rejection(env["db"], f"delete from {table}")


def test_terminal_orders_are_immutable_and_orders_cannot_be_deleted(env):
    r = _filled(env)
    _expect_db_rejection(env["db"], "update orders set status = 'CANCELLED', cancel_reason = 'x' where id = :i", i=r.order.id)
    _expect_db_rejection(env["db"], "delete from orders")


@pytest.mark.parametrize(
    "assignment", ["quantity = 4", "side = 'SELL'", "accepted_at = accepted_at - interval '1 hour'", "risk_budget = 1"]
)
def test_order_identity_is_immutable(env, assignment):
    reference(env, "102")
    r = go(env, intent(env["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    _expect_db_rejection(env["db"], f"update orders set {assignment} where id = :i", i=r.order.id)


def test_database_refuses_a_fill_priced_from_unverified_data(env):
    _filled(env)
    _expect_db_rejection(
        env["db"],
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, price_as_of, price_observed_at,"
        " cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity, reference_price,"
        " price, turnover, fee, price_source_kind, 'x', provider_code, 'UNVERIFIED_PROVIDER_NATIVE', price_as_of,"
        " price_observed_at, cost_model, executed_at from fills limit 1",
    )


def test_database_refuses_a_fill_using_a_future_observation(env):
    _filled(env)
    _expect_db_rejection(
        env["db"],
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, price_as_of, price_observed_at,"
        " cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity, reference_price,"
        " price, turnover, fee, price_source_kind, 'y', provider_code, price_basis,"
        " executed_at + interval '1 minute', price_observed_at, cost_model, executed_at from fills limit 1",
    )


def test_position_cannot_go_short_at_the_database_level(env):
    _filled(env)
    _expect_db_rejection(env["db"], "update positions set quantity = -1")


def test_users_cannot_see_each_others_trading_records(env):
    db = env["db"]
    _filled(env)
    other = make_account(db)
    c = ctx(env)
    assert c.portfolio.open_positions(other.user_id) == []
    assert c.portfolio.cash_balance(other) == D("100000")
    order_id = db.scalar(select(Order.id))
    with pytest.raises(OrderNotFound):
        c.exchange.cancel(other, order_id, "not mine")


# ---- migrations ---------------------------------------------------------------------------------------


def _downgrade(engine, target):
    from alembic import command

    from tests.conftest import alembic_config

    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), target)


def _upgrade(engine):
    from alembic import command

    from tests.conftest import alembic_config

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")


def _version(engine):
    with engine.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def test_downgrading_0005_is_refused_while_orders_exist(env, engine):
    _filled(env)
    with pytest.raises(RuntimeError, match="cannot downgrade 0008"):
        _downgrade(engine, "0004")
    assert _version(engine) == "0008"
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from fills")).scalar() == 1


def test_downgrading_0004_is_refused_while_risk_records_exist(env, engine):
    # risk settings exist (env fixture) but no orders: 0005 may go, 0004 must refuse
    try:
        with pytest.raises(RuntimeError, match="cannot downgrade 0004"):
            _downgrade(engine, "0003")
    finally:
        _upgrade(engine)
    assert _version(engine) == "0008"


def test_0005_backfills_accepted_at_for_existing_terminal_orders(env, engine):
    """Upgrading a database that already holds Phase 4 orders (including terminal ones) must succeed."""
    env["db"].close()
    _downgrade(engine, "0004")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "insert into orders (id, user_id, account_id, instrument_id, client_order_id, request_fingerprint,"
                " source, side, order_type, requested_quantity, quantity, status, reject_reason, created_at)"
                " select gen_random_uuid(), a.user_id, a.id, i.id, 'legacy', 'fp', 'MANUAL', 'BUY', 'MARKET', 1, 0,"
                " 'REJECTED', 'legacy', timestamptz '2026-01-02 04:00+00' from virtual_accounts a, instruments i limit 1"
            ))
    finally:
        _upgrade(engine)
    assert _version(engine) == "0008"
    with engine.connect() as conn:
        accepted, created, signal, policy = conn.execute(text(
            "select accepted_at, created_at, signal_at, execution_policy from orders where client_order_id='legacy'"
        )).one()
    assert accepted == created == signal  # 0005 and 0006 backfills
    assert policy is None  # never filled: no policy is invented for it
