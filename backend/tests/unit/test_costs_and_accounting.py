"""Fee, slippage and position accounting arithmetic (pure, Decimal-exact)."""

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.enums import OrderSide, PriceBasis
from app.exchange.accounting import AccountingError, PositionState, apply_buy, apply_sell, unrealized_pnl
from app.exchange.costs import CostModel

D = Decimal
# SYNTHETIC rates chosen for hand calculation, not real NSE/broker charges.
COSTS = CostModel(
    brokerage_bps=D("10"), brokerage_max_per_order=D("20"), stt_buy_bps=D("0"), stt_sell_bps=D("25"),
    txn_charge_bps=D("3"), sebi_fee_bps=D("0"), stamp_duty_buy_bps=D("3"), gst_pct=D("18"), slippage_bps=D("5"),
)
ZERO = CostModel(*(D(0),) * 9)


# ---- slippage -----------------------------------------------------------------------------------


def test_slippage_is_adverse_for_both_sides():
    assert COSTS.slipped_price(OrderSide.BUY, D("100")) == D("100.050000")
    assert COSTS.slipped_price(OrderSide.SELL, D("110")) == D("109.945000")


def test_slipped_prices_round_against_the_trader():
    from dataclasses import replace

    c = replace(ZERO, slippage_bps=D("3"))
    # 123.4567 * 1.0003 = 123.49373701 -> BUY rounds up, SELL rounds down at 6 dp
    assert c.slipped_price(OrderSide.BUY, D("123.4567")) == D("123.493738")
    assert c.slipped_price(OrderSide.SELL, D("123.4567")) == D("123.419662")


# ---- component fees ------------------------------------------------------------------------------


def test_buy_fee_components():
    f = COSTS.fees(OrderSide.BUY, D("1000.5"))
    assert f.brokerage == D("1.000500")  # 10 bps, below the 20 cap
    assert f.stt == D("0")  # buy-side STT set to 0 in this fixture
    assert f.txn_charge == D("0.300150")
    assert f.sebi_fee == D("0")
    assert f.stamp_duty == D("0.300150")  # buy side only
    assert f.gst == D("0.234117")  # 18 % of (brokerage + txn + sebi) = 0.234117
    assert f.total == D("1.834917")


def test_sell_fee_components_have_stt_and_no_stamp_duty():
    f = COSTS.fees(OrderSide.SELL, D("1099.45"))
    assert (f.brokerage, f.stt, f.txn_charge, f.stamp_duty) == (D("1.099450"), D("2.748625"), D("0.329835"), D("0"))
    assert f.gst == D("0.257272")  # 0.2572713 rounded up
    assert f.total == D("4.435182")


def test_brokerage_is_capped_per_order_across_partial_fills():
    big = D("50000")  # 10 bps = 50 > cap 20
    assert COSTS.fees(OrderSide.BUY, big).brokerage == D("20")
    assert COSTS.fees(OrderSide.BUY, big, brokerage_already_charged=D("15")).brokerage == D("5")
    assert COSTS.fees(OrderSide.BUY, big, brokerage_already_charged=D("20")).brokerage == D("0")
    assert COSTS.fees(OrderSide.BUY, big, brokerage_already_charged=D("25")).brokerage == D("0")


def test_each_component_rounds_up_at_six_decimals():
    from dataclasses import replace

    c = replace(ZERO, txn_charge_bps=D("1"))
    assert c.fees(OrderSide.BUY, D("0.011")).txn_charge == D("0.000002")  # 0.0000011 -> up


def test_fee_breakdown_serialises_with_total():
    d = COSTS.fees(OrderSide.BUY, D("1000.5")).as_dict()
    assert d["total"] == "1.834917" and set(d) == {"brokerage", "stt", "txn_charge", "sebi_fee", "stamp_duty", "gst", "total"}


def test_zero_cost_model_is_allowed_only_when_explicit():
    assert ZERO.slipped_price(OrderSide.BUY, D("100")) == D("100")
    assert ZERO.fee(OrderSide.SELL, D("500")) == D("0")


@pytest.mark.parametrize("cash", ["50", "1002.335417", "1002.33", "1021", "5000", "100000", "1234567.89"])
@pytest.mark.parametrize("price", ["100.05", "99", "2500.123456"])
def test_max_buy_quantity_is_affordable_and_maximal_enough(price, cash):
    p, c = D(price), D(cash)
    q = COSTS.max_buy_quantity(p, c)
    if q:
        turnover = (p * q).quantize(D("0.000001"))
        assert turnover + COSTS.fee(OrderSide.BUY, turnover) <= c  # never unaffordable
    # and it is not far from the true maximum (the bound is conservative by at most one share here)
    q2 = q + 2
    turnover2 = (p * q2).quantize(D("0.000001"))
    assert turnover2 + COSTS.fee(OrderSide.BUY, turnover2) > c


def test_buy_cost_per_share_upper_bounds_the_real_cost():
    per_share = COSTS.buy_cost_per_share_upper(D("100.05"))
    for q in (1, 10, 1000, 100000):
        turnover = D("100.05") * q
        assert turnover + COSTS.fee(OrderSide.BUY, turnover) <= per_share * q + D("0.00001")


def _settings(**over) -> Settings:
    base = dict(
        db_host="h", db_port=1, db_user="u", db_password="p", db_name="n", db_test_name="n_test",
        jwt_secret_key="x" * 40, access_token_expire_minutes=5,
    )
    return Settings(**{**base, **over})


_ALL_COSTS = dict(
    exchange_brokerage_bps="10", exchange_brokerage_max_per_order="20", exchange_stt_buy_bps="0",
    exchange_stt_sell_bps="25", exchange_txn_charge_bps="3", exchange_sebi_fee_bps="0",
    exchange_stamp_duty_buy_bps="3", exchange_gst_pct="18", exchange_slippage_bps="5",
)


def test_cost_model_is_none_until_every_rate_is_configured():
    assert CostModel.from_settings(_settings()) is None
    assert len(CostModel.missing_settings(_settings())) == 9
    for key in _ALL_COSTS:
        partial = {k: v for k, v in _ALL_COSTS.items() if k != key}
        s = _settings(**partial)
        assert CostModel.from_settings(s) is None
        assert CostModel.missing_settings(s) == [key.upper()]
    assert CostModel.from_settings(_settings(**_ALL_COSTS)) == COSTS


def test_execution_defaults_fail_closed():
    s = _settings()
    assert s.execution_price_source == "disabled"
    assert s.execution_approved_price_bases == []
    assert s.execution_approved_providers == []


def test_unverified_basis_can_never_be_approved_in_configuration():
    with pytest.raises(ValueError, match="UNVERIFIED_PROVIDER_NATIVE"):
        _settings(execution_approved_price_bases=[PriceBasis.UNVERIFIED_PROVIDER_NATIVE])


def test_the_retired_same_bar_close_source_cannot_be_configured():
    with pytest.raises(ValueError):
        _settings(execution_price_source="candle_close")


def test_daily_timeframe_cannot_be_the_execution_timeframe():
    with pytest.raises(ValueError, match="intraday"):
        _settings(execution_candle_timeframe="1d")


# ---- position accounting --------------------------------------------------------------------


FLAT = PositionState(0, None)


def test_average_entry_price_is_volume_weighted():
    s = apply_buy(FLAT, 10, D("100"))
    s = apply_buy(s, 30, D("104"))
    assert s.quantity == 40
    assert s.avg_entry_price == D("103")  # (1000 + 3120) / 40


def test_average_price_keeps_ten_decimal_places():
    s = apply_buy(apply_buy(FLAT, 1, D("100")), 2, D("100.01"))
    assert s.avg_entry_price == D("100.0066666667")


def test_partial_sell_realizes_pnl_and_keeps_average():
    s = apply_buy(FLAT, 10, D("100.05"))
    s, realized = apply_sell(s, 4, D("109.945"))
    assert realized == D("39.580000")
    assert s.quantity == 6 and s.avg_entry_price == D("100.05")
    s, realized2 = apply_sell(s, 6, D("90"))
    assert realized2 == D("-60.300000")
    assert s.quantity == 0 and s.avg_entry_price is None
    assert s.realized_pnl == D("-20.720000")


def test_unrealized_pnl():
    s = apply_buy(FLAT, 10, D("100.05"))
    assert unrealized_pnl(s, D("101")) == D("9.500000")
    assert unrealized_pnl(FLAT, D("101")) == D(0)


@pytest.mark.parametrize(
    "fn",
    [
        lambda: apply_sell(apply_buy(FLAT, 5, D("10")), 6, D("10")),  # short
        lambda: apply_sell(FLAT, 1, D("10")),
        lambda: apply_buy(FLAT, 0, D("10")),
        lambda: apply_buy(FLAT, 1, D("0")),
        lambda: PositionState(-1, D("1")),
        lambda: PositionState(1, None),
    ],
)
def test_invalid_accounting_operations_raise(fn):
    with pytest.raises(AccountingError):
        fn()
