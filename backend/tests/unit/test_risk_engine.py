"""Pure risk engine: hard limits, hybrid strategy rules, sizing and rejection reasons."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.db.enums import OrderSide, OrderType, RiskOutcome
from tests.cost_fixtures import fixture_costs
from app.risk.engine import AccountRiskSnapshot, TradeProposal, UserRiskLimits, evaluate

D = Decimal
# SYNTHETIC rates (tests/fixtures/cost_schedule_synthetic.json); worst-case buy cost per share at 100.05 is 100.2334917.
COSTS = fixture_costs()
LIMITS = UserRiskLimits(
    max_risk_per_trade_pct=D("1"),
    max_position_allocation_pct=D("20"),
    max_portfolio_exposure_pct=D("100"),
    max_daily_loss_pct=D("5"),
    max_drawdown_pct=D("10"),
    max_position_quantity=None,
    resize_allowed=True,
)
SNAP = AccountRiskSnapshot(
    kill_switch_engaged=False,
    equity=D("100000"),
    equity_detail="equity 100000",
    available_cash=D("100000"),
    gross_exposure=D("0"),
    peak_equity=D("100000"),
    day_start_equity=D("100000"),
    position_quantity=0,
    position_value=D("0"),
    sellable_quantity=0,
    open_buy_quantity=0,
    open_buy_commitment=D("0"),
)
BUY = TradeProposal(
    side=OrderSide.BUY,
    order_type=OrderType.MARKET,
    quantity=10,
    reference_price=D("100"),
    stop_loss_price=D("95"),
)


def run(proposal=BUY, limits=LIMITS, snap=SNAP, costs=COSTS, price_ok=True):
    return evaluate(proposal, limits, snap, costs, price_ok, "eligible" if price_ok else "STALE: too old")


def reason(result) -> str:
    return result.reasons[0].split(":")[0]


def test_compliant_entry_is_approved_with_risk_amount():
    r = run()
    assert r.outcome is RiskOutcome.APPROVED and r.approved_quantity == 10
    assert r.sizing_price == D("100.050000")
    assert r.risk_amount == D("50.500000")  # 10 * (100.05 - 95)


def test_strategy_proposing_more_risk_than_user_allows_is_resized():
    # PRD example: user allows 1% per trade; the oversized proposal is cut to the 1% budget.
    r = run(replace(BUY, quantity=500))
    assert r.outcome is RiskOutcome.RESIZED
    assert r.approved_quantity == 198  # floor(1000 / 5.05)
    assert r.risk_amount <= D("1000")
    assert "max_risk_per_trade" in r.reasons[0]


def test_oversized_proposal_is_rejected_when_resizing_is_not_allowed():
    r = run(replace(BUY, quantity=500), limits=replace(LIMITS, resize_allowed=False))
    assert r.outcome is RiskOutcome.REJECTED and r.approved_quantity == 0
    assert reason(r) == "RESIZE_NOT_ALLOWED"


def test_strategy_limit_can_only_tighten_the_user_limit():
    tighter = run(replace(BUY, quantity=500, strategy_max_risk_pct=D("0.5")))
    assert tighter.approved_quantity == 99  # floor(500 / 5.05)
    assert tighter.effective_risk_pct == D("0.5")

    looser = run(replace(BUY, quantity=500, strategy_max_risk_pct=D("2")))
    assert looser.approved_quantity == 198  # user's 1% still applies
    assert looser.effective_risk_pct == D("1")
    assert any(c.name == "strategy_risk_capped_by_user" for c in looser.checks)


def test_allocation_cap_counts_existing_holding_and_open_buys():
    snap = replace(SNAP, position_value=D("15000"), open_buy_commitment=D("4000"), gross_exposure=D("19000"))
    r = run(replace(BUY, quantity=100), snap=snap)
    # 20% of 100000 = 20000; remaining 1000 / 100.2334917 -> 9
    assert r.outcome is RiskOutcome.RESIZED and r.approved_quantity == 9


def test_exposure_cap():
    r = run(replace(BUY, quantity=100), limits=replace(LIMITS, max_portfolio_exposure_pct=D("50")),
            snap=replace(SNAP, gross_exposure=D("49500")))
    assert r.approved_quantity == 4  # 500 / 100.2334917


def test_cash_cap_includes_every_fee():
    r = run(replace(BUY, quantity=100), snap=replace(SNAP, available_cash=D("1021")))
    assert r.approved_quantity == 10  # 10 shares cost 1000.5 + 1.834917 fees; 11 would cost > 1021


def test_quantity_cap_counts_held_and_open_buy_quantity():
    r = run(replace(BUY, quantity=50), limits=replace(LIMITS, max_position_quantity=60),
            snap=replace(SNAP, position_quantity=40, position_value=D("4000"), gross_exposure=D("4000"),
                         open_buy_quantity=15))
    assert r.approved_quantity == 5


def test_lot_size_rounds_down():
    r = run(replace(BUY, quantity=500, lot_size=25))
    assert r.approved_quantity == 175  # 198 rounded down to a multiple of 25


def test_no_compliant_size_is_rejected():
    r = run(replace(BUY, quantity=5), snap=replace(SNAP, available_cash=D("50")))
    assert r.outcome is RiskOutcome.REJECTED and reason(r) == "SIZE_EXCEEDS_LIMITS"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(price_ok=False), "NO_ELIGIBLE_PRICE"),
        (dict(costs=None), "COSTS_NOT_CONFIGURED"),
        (dict(limits=None), "RISK_SETTINGS_NOT_CONFIGURED"),
        (dict(snap=replace(SNAP, kill_switch_engaged=True)), "KILL_SWITCH_ENGAGED"),
        (dict(proposal=replace(BUY, stop_loss_price=None)), "STOP_LOSS_REQUIRED"),
        (dict(proposal=replace(BUY, stop_loss_price=D("100.05"))), "STOP_LOSS_NOT_BELOW_ENTRY"),
        (dict(proposal=replace(BUY, take_profit_price=D("100"))), "TAKE_PROFIT_NOT_ABOVE_ENTRY"),
        (dict(snap=replace(SNAP, equity=None, equity_detail="cannot mark")), "EQUITY_UNDETERMINED"),
        (dict(snap=replace(SNAP, equity=D("90000"), peak_equity=D("100000"), day_start_equity=None)),
         "MAX_DRAWDOWN_BREACHED"),
        (dict(snap=replace(SNAP, equity=D("95000"), peak_equity=D("95000"), day_start_equity=D("100000"))),
         "DAILY_LOSS_LIMIT_BREACHED"),
    ],
)
def test_entry_rejections(kwargs, expected):
    r = run(**kwargs)
    assert r.outcome is RiskOutcome.REJECTED and r.approved_quantity == 0
    assert reason(r) == expected


def test_limits_just_inside_the_threshold_pass():
    # drawdown 9.999 % < 10 %; daily loss 4736 / 94737 = 4.9991 % < 5 %
    snap = replace(SNAP, equity=D("90001"), peak_equity=D("100000"), day_start_equity=D("94737"))
    assert run(snap=snap).approved


def test_limit_buy_is_sized_at_its_limit_price():
    r = run(
        replace(BUY, order_type=OrderType.LIMIT, limit_price=D("99"), quantity=1000),
        limits=replace(LIMITS, max_position_allocation_pct=D("100")),  # isolate the risk cap
    )
    assert r.sizing_price == D("99")  # never fills above the limit
    assert r.approved_quantity == 250  # floor(1000 / (99 - 95))


def test_exit_of_held_position_is_allowed_even_under_kill_switch_and_breached_limits():
    sell = TradeProposal(OrderSide.SELL, OrderType.MARKET, 10, D("100"))
    snap = replace(SNAP, kill_switch_engaged=True, equity=D("50000"), position_quantity=10, sellable_quantity=10)
    r = run(sell, limits=None, snap=snap)
    assert r.outcome is RiskOutcome.APPROVED and r.approved_quantity == 10


def test_short_selling_is_rejected():
    sell = TradeProposal(OrderSide.SELL, OrderType.MARKET, 11, D("100"))
    r = run(sell, snap=replace(SNAP, position_quantity=10, sellable_quantity=10))
    assert reason(r) == "INSUFFICIENT_POSITION"


def test_exit_still_requires_an_eligible_price():
    sell = TradeProposal(OrderSide.SELL, OrderType.MARKET, 10, None)
    assert reason(run(sell, snap=replace(SNAP, sellable_quantity=10), price_ok=False)) == "NO_ELIGIBLE_PRICE"


def test_every_check_is_recorded():
    names = {c.name for c in run().checks}
    assert {"price_eligible", "protective_levels", "max_drawdown", "daily_loss", "max_risk_per_trade",
            "max_position_allocation", "max_portfolio_exposure", "available_cash"} <= names
