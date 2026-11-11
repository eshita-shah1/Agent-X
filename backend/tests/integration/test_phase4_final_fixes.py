"""B10 (simulated fill time) and B4 (corporate-action recovery), on SYNTHETIC provider-native fixture bars with the
synthetic cost schedule. These prove the accounting rules; they are not evidence about real Yahoo data or brokers."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import AdjustmentStatus, CorporateActionType, OrderSide, OrderStatus, OrderType
from app.db.models import (
    CashLedgerEntry,
    CorporateAction,
    CorporateActionAdjustment,
    Fill,
    Position,
)
from app.exchange.corporate_actions import CorporateActionService
from app.exchange.portfolio import daily_pnl
from app.orders.service import TradingContext
from tests.integration.trading_support import (
    IST,
    NOW,
    Clock,
    configure_risk,
    intent,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    next_bar,
    process,
    simulation_settings,
    submit,
)

D = Decimal
JAN5, JAN6 = date(2026, 1, 5), date(2026, 1, 6)


@pytest.fixture
def s(db, test_settings):
    provider = make_provider(db)
    inst = make_instrument(db)
    account = make_account(db)
    configure_risk(db, account, max_position_allocation_pct=D("100"), max_risk_per_trade_pct=D("100"))
    return dict(db=db, provider=provider, inst=inst, account=account, settings=simulation_settings(test_settings),
                clock=Clock())


def ref(s, close="100", inst=None, **kw):
    return make_candle(s["db"], s["provider"], inst or s["inst"], close, verified=False, **kw)


def bar(s, open_, close=None, inst=None, **kw):
    return next_bar(s["db"], s["provider"], inst or s["inst"], s["clock"], open_, close, verified=False, **kw)


def go(s, order_intent):
    return submit(s["db"], s["settings"], s["clock"], s["account"], order_intent)


def run(s):
    return process(s["db"], s["settings"], s["clock"], s["account"])


def ctx(s):
    return TradingContext.build(s["db"], s["settings"], s["clock"])


def cas(s):
    c = ctx(s)
    return CorporateActionService(s["db"], c.exchange, s["clock"])


def at(day: date, hh: int, mm: int):
    from datetime import datetime

    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


def split(s, ex: date, num="2", den="1", kind=CorporateActionType.SPLIT, inst=None):
    a = CorporateAction(
        instrument_id=(inst or s["inst"]).id, action_type=kind, ex_date=ex,
        ratio_numerator=D(num) if num else None, ratio_denominator=D(den) if den else None,
        source="fixture corporate action", provider_id=s["provider"].id, observed_at=NOW,
    )
    s["db"].add(a)
    s["db"].commit()
    return a


def buy_and_fill(s, qty=10, open_="100", stop="95"):
    ref(s)
    r = go(s, intent(s["inst"], qty=qty, stop_loss_price=D(stop)))
    bar(s, open_)
    run(s)
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.FILLED, r.order.cancel_reason
    return r.order


def snapshot_immutables(db):
    fills = [tuple(r) for r in db.execute(text("select id, quantity, price, fee, realized_pnl from fills order by id"))]
    ledger = [tuple(r) for r in db.execute(text("select id, entry_type, amount from cash_ledger order by id"))]
    return fills, ledger


def cash(s):
    return ctx(s).portfolio.cash_balance(s["account"])


# =========================================== B10 ============================================================


def test_same_day_fill_opens_the_position_at_the_simulated_fill_time(s):
    buy_and_fill(s)
    f = s["db"].scalar(select(Fill))
    pos = s["db"].scalar(select(Position))
    assert pos.opened_at == f.price_as_of == NOW  # bar start of the 05:00 UTC bar
    assert f.executed_at == s["clock"].now and f.price_observed_at <= f.executed_at
    assert f.executed_at != f.price_as_of  # the three timestamps stay distinct


@pytest.mark.parametrize("delay_days", [1, 3])
def test_delayed_processing_keeps_the_simulated_open_date(s, delay_days):
    db = s["db"]
    ref(s)
    go(s, intent(s["inst"], qty=10))
    bar(s, "100")  # the 2026-01-05 bar is archived ...
    s["clock"].now = NOW + timedelta(days=delay_days)  # ... but processing only runs days later
    run(s)
    f = db.scalar(select(Fill))
    pos = db.scalar(select(Position))
    assert f.price_as_of == NOW and pos.opened_at == NOW
    assert f.executed_at == NOW + timedelta(days=delay_days)
    assert f.price_observed_at == NOW + timedelta(minutes=5)  # availability preserved separately


def test_regression_fill_processed_on_a_split_ex_date_cannot_book_a_false_loss(s):
    """Review reproduction: a 5 Jan fill processed on 6 Jan (split ex-date) used to open on 6 Jan, escape the
    freeze, and allow a SELL on the post-split scale (Rs 50 vs Rs 100 average: a false 50 % loss)."""
    db = s["db"]
    ref(s)
    go(s, intent(s["inst"], qty=10))
    bar(s, "100")  # 5 Jan bar, pre-split basis
    split(s, JAN6)
    s["clock"].now = at(JAN6, 10, 30)
    run(s)
    pos = db.scalar(select(Position))
    assert pos.opened_at.astimezone(IST).date() == JAN5  # was 6 Jan before the fix
    make_candle(db, s["provider"], s["inst"], "50", end=s["clock"].now - timedelta(minutes=1), verified=False)
    r = go(s, intent(s["inst"], side=OrderSide.SELL, qty=10))
    assert r.order.status is OrderStatus.REJECTED
    assert "CORPORATE_ACTION_UNADJUSTED_POSITION" in r.order.reject_reason
    assert db.scalar(select(func.count()).select_from(Fill).where(Fill.side == OrderSide.SELL)) == 0
    assert db.scalar(select(Position.realized_pnl)) == 0  # no false loss was booked


def test_daily_pnl_is_attributed_to_the_simulated_session(s):
    db = s["db"]
    buy_and_fill(s)  # 5 Jan 05:00 buy, booked on 5 Jan
    sell = go(s, intent(s["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("104")))
    assert sell.order.status is OrderStatus.PENDING, sell.order.reject_reason
    bar(s, "103", high="105")  # the sell crosses 104 on 5 Jan ...
    s["clock"].now = at(JAN6, 11, 0)  # ... but is only processed on 6 Jan
    run(s)
    sell_fill = db.scalar(select(Fill).where(Fill.order_id == sell.order.id))
    assert sell_fill.executed_at.astimezone(IST).date() == JAN6 and sell_fill.price_as_of.astimezone(IST).date() == JAN5
    jan5, jan6 = daily_pnl(db, s["account"].user_id, JAN5), daily_pnl(db, s["account"].user_id, JAN6)
    assert jan5.realized_pnl == D("39.5")  # (104 - 100.05) x 10, on the simulated date
    assert jan5.fills == 2 and jan5.fees > 0 and jan5.net_realized_pnl == jan5.realized_pnl - jan5.fees
    assert (jan6.realized_pnl, jan6.fills, jan6.fees) == (0, 0, 0)


def test_late_processing_books_fills_in_simulated_order_across_orders(s):
    """A resting limit SELL accepted first but filling later must not be booked before an earlier market BUY."""
    db = s["db"]
    buy_and_fill(s)  # hold 10 @ 100.05 (05:00 bar); clock 05:05
    sell = go(s, intent(s["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("105")))
    bar(s, "101", high="102")  # 05:05: sell not marketable; clock 05:10
    buy2 = go(s, intent(s["inst"], qty=10))  # accepted 05:10
    bar(s, "101", high="102")  # 05:10: BUY fills at its open; sell still not marketable
    bar(s, "104", high="106")  # 05:15: SELL crosses 105
    s["clock"].now = at(JAN5, 16, 0)  # one catch-up run after the close
    run(s)
    db.refresh(sell.order)
    db.refresh(buy2.order)
    assert sell.order.status is OrderStatus.FILLED and buy2.order.status is OrderStatus.FILLED, (
        sell.order.cancel_reason, buy2.order.reject_reason, buy2.order.cancel_reason)
    sell_fill = db.scalar(select(Fill).where(Fill.order_id == sell.order.id))
    buy2_fill = db.scalar(select(Fill).where(Fill.order_id == buy2.order.id))
    assert buy2_fill.id < sell_fill.id  # booked in simulated order (05:10 before 05:15)
    avg_after_second_buy = ((D("100.05") * 10 + buy2_fill.price * 10) / 20).quantize(D("0.0000000001"))
    assert sell_fill.realized_pnl == ((D("105") - avg_after_second_buy) * 10).quantize(D("0.000001"))
    pos = db.scalar(select(Position))
    assert pos.quantity == 10 and pos.avg_entry_price == avg_after_second_buy


# =========================================== B4 =============================================================


def frozen_position(s, qty=10, open_="100", stop="95", num="2", den="1", kind=CorporateActionType.SPLIT):
    buy_and_fill(s, qty=qty, open_=open_, stop=stop)
    action = split(s, JAN6, num, den, kind)
    s["clock"].now = at(JAN6, 10, 30)
    return action


CIL_SOURCE = "FIXTURE: company cash-in-lieu announcement"


def adjust(s, action, num=None, den=None, evidence="NSE circular FIXTURE/2026/001", cil=None, cil_source=CIL_SOURCE,
           cil_at=None):
    result = cas(s).adjust(
        s["account"], instrument_id=s["inst"].id, corporate_action_id=action.id, evidence_reference=evidence,
        confirm_ratio_numerator=D(num or action.ratio_numerator or 1),
        confirm_ratio_denominator=D(den or action.ratio_denominator or 1), cash_in_lieu_price=cil,
        cash_in_lieu_source=cil_source if cil is not None else None,
        cash_in_lieu_observed_at=(cil_at or NOW) if cil is not None else None,
    )
    s["db"].commit()
    return result


@pytest.mark.parametrize("num,den,qty,exp_qty,exp_avg", [
    ("2", "1", 10, 20, D("50.025")),
    ("5", "1", 10, 50, D("20.01")),
    ("1", "5", 10, 2, D("500.25")),  # reverse split, exact
])
def test_split_adjustment_preserves_cost_basis_and_leaves_history_untouched(s, num, den, qty, exp_qty, exp_avg):
    db = s["db"]
    action = frozen_position(s, qty=qty, num=num, den=den)
    before_fills, before_ledger = snapshot_immutables(db)
    cash_before = cash(s)
    res = adjust(s, action)
    a = res.adjustment
    assert a.status is AdjustmentStatus.APPLIED and not res.replayed
    assert (a.original_quantity, a.adjusted_quantity) == (qty, exp_qty)
    assert a.adjusted_avg_price == exp_avg
    assert a.original_cost_basis == a.adjusted_cost_basis == D("100.05") * qty
    assert a.fractional_quantity == 0 and a.cash_in_lieu is None
    pos = db.scalar(select(Position))
    assert (pos.quantity, pos.avg_entry_price) == (exp_qty, exp_avg)
    assert pos.stop_loss_price == (D("95") * D(den) / D(num)).quantize(D("0.000001"))
    assert snapshot_immutables(db) == (before_fills, before_ledger)  # fills and ledger never modified
    assert cash(s) == cash_before  # no cash movement


def test_after_adjustment_the_position_can_be_valued_and_closed(s):
    db = s["db"]
    action = frozen_position(s)
    assert ctx(s).portfolio.valuation(s["account"]).equity is None  # frozen
    adjust(s, action)
    make_candle(db, s["provider"], s["inst"], "51", end=s["clock"].now - timedelta(minutes=1), verified=False)
    v = ctx(s).portfolio.valuation(s["account"])
    assert v.equity is not None and v.positions[0].unrealized_pnl == ((D("51") - D("50.025")) * 20).quantize(D("0.000001"))
    r = go(s, intent(s["inst"], side=OrderSide.SELL, qty=20))
    assert r.order.status is OrderStatus.PENDING
    bar(s, "51")
    run(s)
    db.refresh(r.order)
    assert r.order.status is OrderStatus.FILLED
    assert db.scalar(select(Position.quantity)) == 0


def test_a_frozen_position_cannot_be_traded_before_adjustment(s):
    db = s["db"]
    frozen_position(s)
    make_candle(db, s["provider"], s["inst"], "50", end=s["clock"].now - timedelta(minutes=1), verified=False)
    for side in (OrderSide.SELL, OrderSide.BUY):
        r = go(s, intent(s["inst"], side=side, qty=1))
        assert r.order.status is OrderStatus.REJECTED
    assert db.scalar(select(func.count()).select_from(Fill)) == 1  # only the original buy


def test_duplicate_application_is_idempotent(s):
    db = s["db"]
    action = frozen_position(s)
    first = adjust(s, action)
    second = adjust(s, action)
    assert second.replayed and second.adjustment.id == first.adjustment.id
    assert db.scalar(select(Position.quantity)) == 20  # not 40
    assert db.scalar(select(func.count()).select_from(CorporateActionAdjustment)) == 1


def test_database_refuses_a_second_applied_row_and_any_edit(s):
    db = s["db"]
    action = frozen_position(s)
    adjust(s, action)
    for sql in (
        "insert into corporate_action_adjustments (user_id, account_id, position_id, instrument_id,"
        " corporate_action_id, action_type, ratio_numerator, ratio_denominator, ex_date, source, evidence_reference,"
        " evidence, status, original_quantity, original_avg_price, original_cost_basis, adjusted_quantity,"
        " adjusted_avg_price, adjusted_cost_basis, fractional_quantity, requested_by, adjusted_at)"
        " select user_id, account_id, position_id, instrument_id, corporate_action_id, action_type, ratio_numerator,"
        " ratio_denominator, ex_date, source, evidence_reference, evidence, 'APPLIED', original_quantity,"
        " original_avg_price, original_cost_basis, adjusted_quantity, adjusted_avg_price, adjusted_cost_basis,"
        " fractional_quantity, requested_by, adjusted_at from corporate_action_adjustments",
        "update corporate_action_adjustments set adjusted_quantity = 99",
        "delete from corporate_action_adjustments",
    ):
        with pytest.raises((IntegrityError, DBAPIError)):
            db.execute(text(sql))
            db.commit()
        db.rollback()


@pytest.mark.parametrize("num,den,reason", [("3", "1", "RATIO_MISMATCH"), ("2", "3", "RATIO_MISMATCH")])
def test_an_incorrect_confirmed_ratio_is_refused_and_recorded(s, num, den, reason):
    db = s["db"]
    action = frozen_position(s)
    res = adjust(s, action, num=num, den=den)
    assert res.adjustment.status is AdjustmentStatus.REJECTED and res.adjustment.reason.startswith(reason)
    assert db.scalar(select(Position.quantity)) == 10  # still frozen, unchanged
    assert ctx(s).portfolio.valuation(s["account"]).equity is None


def test_missing_evidence_and_non_adjustable_actions_are_refused(s):
    action = frozen_position(s)
    assert adjust(s, action, evidence="   ").adjustment.reason.startswith("EVIDENCE_REQUIRED")
    other = split(s, JAN6 + timedelta(days=0), num=None, den=None, kind=CorporateActionType.OTHER)
    assert other.ratio_numerator is None
    # the SPLIT (lower id, same ex-date) is the earliest pending action; OTHER must wait and is refused out of order
    res = cas(s).adjust(s["account"], instrument_id=s["inst"].id, corporate_action_id=other.id,
                        evidence_reference="x", confirm_ratio_numerator=D(1), confirm_ratio_denominator=D(1))
    s["db"].commit()
    assert res.adjustment.status is AdjustmentStatus.REJECTED
    assert res.adjustment.reason.startswith(("OUT_OF_ORDER", "NOT_ADJUSTABLE"))


def test_an_other_type_action_is_never_auto_adjusted(s):
    db = s["db"]
    buy_and_fill(s)
    other = split(s, JAN6, num=None, den=None, kind=CorporateActionType.OTHER)
    s["clock"].now = at(JAN6, 10, 30)
    res = cas(s).adjust(s["account"], instrument_id=s["inst"].id, corporate_action_id=other.id,
                        evidence_reference="x", confirm_ratio_numerator=D(1), confirm_ratio_denominator=D(1))
    db.commit()
    assert res.adjustment.reason.startswith("NOT_ADJUSTABLE")
    pending = cas(s).pending(s["account"].user_id)
    assert len(pending) == 1 and pending[0].adjustable is False


def test_actions_apply_in_ex_date_order(s):
    db = s["db"]
    buy_and_fill(s)
    first = split(s, JAN6)
    second = split(s, date(2026, 1, 7), "5", "1")
    s["clock"].now = at(date(2026, 1, 7), 10, 30)
    assert adjust(s, second).adjustment.reason.startswith("OUT_OF_ORDER")
    assert adjust(s, first).adjustment.status is AdjustmentStatus.APPLIED
    assert adjust(s, second).adjustment.status is AdjustmentStatus.APPLIED
    assert db.scalar(select(Position.quantity)) == 100  # 10 x 2 x 5


def test_fractional_entitlement_requires_cash_in_lieu_and_is_never_rounded_silently(s):
    db = s["db"]
    action = frozen_position(s, qty=12, num="1", den="5")  # 12 / 5 = 2.4 shares
    blocked = adjust(s, action)
    assert blocked.adjustment.status is AdjustmentStatus.BLOCKED_FRACTIONAL
    assert "CASH_IN_LIEU_PRICE_REQUIRED" in blocked.adjustment.reason
    assert db.scalar(select(Position.quantity)) == 12 and cas(s).pending(s["account"].user_id)[0].last_attempt.status \
        is AdjustmentStatus.BLOCKED_FRACTIONAL
    cash_before = cash(s)
    fills_before, ledger_before = snapshot_immutables(db)
    # a cash-in-lieu price without provenance is refused (never estimated, never anonymous)
    no_source = adjust(s, action, cil=D("510"), cil_source=None).adjustment
    assert no_source.status is AdjustmentStatus.BLOCKED_FRACTIONAL
    assert no_source.reason.startswith("CASH_IN_LIEU_PROVENANCE_REQUIRED")
    applied = adjust(s, action, cil=D("510")).adjustment
    assert applied.status is AdjustmentStatus.APPLIED
    assert applied.cash_in_lieu_source == CIL_SOURCE and applied.cash_in_lieu_observed_at == NOW
    assert (applied.adjusted_quantity, applied.fractional_quantity) == (2, D("0.4"))
    assert applied.adjusted_avg_price == D("500.25")  # 100.05 x 5
    assert applied.cash_in_lieu == D("204")  # 0.4 x 510
    assert applied.realized_pnl == D("3.9")  # 204 - 0.4 x 500.25
    assert applied.adjusted_cost_basis + applied.fractional_quantity * applied.adjusted_avg_price == D("100.05") * 12
    assert cash(s) == cash_before + D("204")  # the only cash movement, separately recorded
    cil = db.scalar(select(CashLedgerEntry).where(CashLedgerEntry.adjustment_id == applied.id))
    assert cil.entry_type.value == "CASH_IN_LIEU" and cil.fill_id is None
    fills_after, ledger_after = snapshot_immutables(db)
    assert fills_after == fills_before and ledger_after[: len(ledger_before)] == ledger_before
    assert daily_pnl(db, s["account"].user_id, JAN6).realized_pnl == D("3.9")


def test_cash_in_lieu_is_refused_when_there_is_no_fraction(s):
    action = frozen_position(s)
    assert adjust(s, action, cil=D("50")).adjustment.reason.startswith("CASH_IN_LIEU_NOT_APPLICABLE")


def test_a_failure_during_adjustment_rolls_back_everything(s, monkeypatch):
    db = s["db"]
    action = frozen_position(s, qty=12, num="1", den="5")
    before = snapshot_immutables(db)

    from app.exchange import corporate_actions as module

    def boom(*a, **k):
        raise RuntimeError("injected failure while writing the cash-in-lieu entry")

    monkeypatch.setattr(module, "CashLedgerEntry", boom)
    with pytest.raises(RuntimeError):
        cas(s).adjust(s["account"], instrument_id=s["inst"].id, corporate_action_id=action.id,
                      evidence_reference="x", confirm_ratio_numerator=D(1), confirm_ratio_denominator=D(5),
                      cash_in_lieu_price=D("510"), cash_in_lieu_source=CIL_SOURCE, cash_in_lieu_observed_at=NOW)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(CorporateActionAdjustment)) == 0
    pos = db.scalar(select(Position))
    assert (pos.quantity, pos.avg_entry_price) == (12, D("100.05"))
    assert snapshot_immutables(db) == before


def test_unaffected_instruments_keep_their_exits_while_one_position_is_frozen(s):
    db = s["db"]
    other = make_instrument(db, "OTHERCO")
    ref(s, inst=other)
    buy_and_fill(s)  # TESTCO
    r = go(s, intent(other, qty=5))
    bar(s, "100", inst=other)
    run(s)
    db.refresh(r.order)
    assert r.order.status is OrderStatus.FILLED
    split(s, JAN6)  # TESTCO only
    s["clock"].now = at(JAN6, 10, 30)
    make_candle(db, s["provider"], other, "101", end=s["clock"].now - timedelta(minutes=1), verified=False)
    make_candle(db, s["provider"], s["inst"], "50", end=s["clock"].now - timedelta(minutes=1), verified=False)
    exit_other = go(s, intent(other, side=OrderSide.SELL, qty=5))
    assert exit_other.order.status is OrderStatus.PENDING  # unaffected exits still work
    v = ctx(s).portfolio.valuation(s["account"])
    marks = {p.position.instrument_id: p for p in v.positions}
    assert marks[other.id].mark == D("101") and marks[s["inst"].id].mark is None
    # new entries anywhere need a determined equity, which the frozen position prevents (documented)
    entry = go(s, intent(other, qty=1))
    assert entry.order.status is OrderStatus.REJECTED and "EQUITY_UNDETERMINED" in entry.order.reject_reason


# =========================================== API ===========================================================


def test_corporate_action_endpoints_are_operator_only_and_users_see_their_own(s, test_settings):
    from fastapi.testclient import TestClient

    from app.db.models import AuditEvent, User, VirtualAccount
    from app.main import create_app
    from tests.conftest import auth_headers, login, register_user

    app = create_app(simulation_settings(test_settings))
    app.state.clock = s["clock"]
    db = s["db"]
    with TestClient(app) as c:
        info = register_user(c)
        h = auth_headers(login(c, info["email"]))
        acct = db.scalar(select(VirtualAccount).where(VirtualAccount.id == info["account"]["id"]))
        configure_risk(db, acct, max_position_allocation_pct=D("100"))
        action = frozen_position(dict(s, account=acct))
        mine = c.get("/api/v1/portfolio/corporate-actions/pending", headers=h).json()
        assert mine[0]["corporate_action_id"] == action.id and mine[0]["adjustable"] is True
        body = dict(evidence_reference="NSE/FIXTURE/1", confirm_ratio_numerator="2", confirm_ratio_denominator="1")
        url = f"/api/v1/operator/corporate-actions/{action.id}/adjustments"
        # the position owner cannot adjust their own portfolio
        assert c.post(url, json=body, headers=h).status_code == 403
        assert c.get("/api/v1/operator/corporate-actions/pending", headers=h).status_code == 403
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "operator.access_denied")) == 2
        assert db.scalar(select(func.count()).select_from(CorporateActionAdjustment)) == 0
        # an operator (flag set server-side, as the CLI does) applies it for every affected user
        op_info = register_user(c)
        db.execute(text("update users set is_operator = true where email = :e"), {"e": op_info["email"]})
        db.commit()
        ho = auth_headers(login(c, op_info["email"]))
        pend = c.get("/api/v1/operator/corporate-actions/pending", headers=ho).json()
        assert pend[0]["corporate_action_id"] == action.id and pend[0]["affected_positions"] == 1
        bad = c.post(url, json=dict(body, confirm_ratio_numerator="3"), headers=ho).json()
        assert bad[0]["adjustment"]["status"] == "REJECTED"
        ok = c.post(url, json=body, headers=ho).json()
        assert ok[0]["adjustment"]["status"] == "APPLIED" and ok[0]["adjustment"]["adjusted_quantity"] == 20
        assert ok[0]["user_id"] == info["user"]["id"]
        operator_id = db.scalar(select(User.id).where(User.email == op_info["email"]))
        assert ok[0]["adjustment"]["requested_by"] == str(operator_id)
        assert c.post(url, json=body, headers=ho).json() == []  # nothing left frozen: idempotent
        assert len(c.get("/api/v1/portfolio/corporate-actions/adjustments", headers=h).json()) == 2  # owner's view
        assert c.get("/api/v1/portfolio/corporate-actions/pending", headers=h).json() == []
        assert c.get("/api/v1/portfolio/corporate-actions/adjustments", headers=ho).json() == []  # operator owns none
        daily = c.get("/api/v1/portfolio/pnl/daily", params={"session_date": "2026-01-05"}, headers=h).json()
        assert daily["fills"] == 1 and daily["unattributed_legacy_fills"] == 0 and daily["dividend_income"] == "0"
        assert c.post(url, json=body).status_code == 401


def test_adjustment_cancels_orders_priced_on_the_old_basis(s):
    db = s["db"]
    buy_and_fill(s)
    resting = go(s, intent(s["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("150")))
    assert resting.order.status is OrderStatus.PENDING
    action = split(s, JAN6)
    s["clock"].now = at(JAN6, 10, 30)  # the resting order was never processed after the ex-date
    a = adjust(s, action).adjustment
    db.refresh(resting.order)
    assert resting.order.status is OrderStatus.CANCELLED
    assert resting.order.cancel_reason.startswith("CORPORATE_ACTION_ADJUSTMENT")
    assert a.cancelled_order_ids == [str(resting.order.id)]


# =========================================== B3 in the exchange =============================================


def test_a_fill_uses_the_rates_of_its_simulated_date_not_the_processing_date(s, tmp_path):
    import json

    from tests.cost_fixtures import fixture_schedule_dict

    d = fixture_schedule_dict()
    for r in d["rates"]:
        if r["rate_id"] == "T-STT-DS":
            r["effective_to"] = "2026-01-06"
    d["rates"].append({"rate_id": "T-STT-DS-NEW", "product": "DELIVERY", "charge_type": "STT", "side": "SELL",
                       "basis": "TURNOVER_BPS", "value": "100", "effective_from": "2026-01-06",
                       "source": "synthetic rate change"})
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(d))
    s["settings"] = s["settings"].model_copy(update={"exchange_cost_schedule_path": str(path)})
    db = s["db"]
    buy_and_fill(s)
    sell = go(s, intent(s["inst"], side=OrderSide.SELL, qty=10, otype=OrderType.LIMIT, limit_price=D("104")))
    bar(s, "103", high="105")  # fills on 5 Jan ...
    s["clock"].now = at(JAN6, 11, 0)  # ... processed on 6 Jan, when the new rate is in force
    run(s)
    f = db.scalar(select(Fill).where(Fill.order_id == sell.order.id))
    assert f.fee_breakdown["components"]["STT"]["rate_id"] == "T-STT-DS"
    assert D(f.fee_breakdown["components"]["STT"]["charged"]) == D("2.6")  # 1040 x 25 bps, not 100 bps
    assert f.cost_model["rate_date"] == "2026-01-05"


def test_intraday_orders_are_not_executed(s):
    from app.db.enums import Product

    ref(s)
    r = go(s, intent(s["inst"], product=Product.INTRADAY))
    assert r.order.status is OrderStatus.REJECTED and "PRODUCT_NOT_SUPPORTED" in r.order.reject_reason


def test_dp_charge_is_booked_once_per_instrument_per_simulated_sell_day(s, tmp_path):
    import json

    from tests.cost_fixtures import modified

    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(modified(**{"T-DP-D": {"value": "15"}})))
    s["settings"] = s["settings"].model_copy(update={"exchange_cost_schedule_path": str(path)})
    db = s["db"]
    buy_and_fill(s, qty=30)
    for _ in range(2):  # two separate sells on 5 Jan
        go(s, intent(s["inst"], side=OrderSide.SELL, qty=10))
        bar(s, "100")
        run(s)
    go(s, intent(s["inst"], side=OrderSide.SELL, qty=10))  # accepted 5 Jan ...
    bar(s, "100")
    s["clock"].now = at(JAN6, 11, 0)  # ... processed on 6 Jan: still a 5 Jan (simulated) sell
    run(s)
    dp = [D(f.fee_breakdown["components"]["DP_CHARGE"]["charged"])
          for f in db.scalars(select(Fill).where(Fill.side == OrderSide.SELL).order_by(Fill.id))]
    assert dp == [D("15"), D("0"), D("0")]
