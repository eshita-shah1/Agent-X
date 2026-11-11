"""Fee, slippage and position accounting arithmetic (pure, Decimal-exact)."""

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.enums import OrderSide, PriceBasis
from app.exchange.accounting import AccountingError, PositionState, apply_buy, apply_sell, unrealized_pnl
from app.exchange.costs import CostModel

D = Decimal
COSTS = CostModel(fee_fixed_per_order=D("20"), fee_buy_bps=D("10"), fee_sell_bps=D("30"), slippage_bps=D("5"))


# ---- slippage / fees -----------------------------------------------------------------------


def test_slippage_is_adverse_for_both_sides():
    assert COSTS.slipped_price(OrderSide.BUY, D("100")) == D("100.050000")
    assert COSTS.slipped_price(OrderSide.SELL, D("110")) == D("109.945000")


def test_slipped_prices_round_against_the_trader():
    c = CostModel(D(0), D(0), D(0), slippage_bps=D("3"))
    # 123.4567 * 1.0003 = 123.49373701 -> BUY rounds up, SELL rounds down at 6 dp
    assert c.slipped_price(OrderSide.BUY, D("123.4567")) == D("123.493738")
    assert c.slipped_price(OrderSide.SELL, D("123.4567")) == D("123.419662")


def test_fees_are_exact_side_specific_and_fixed_fee_is_optional():
    assert COSTS.fee(OrderSide.BUY, D("1000.5"), include_fixed=True) == D("21.000500")
    assert COSTS.fee(OrderSide.SELL, D("1099.45"), include_fixed=True) == D("23.298350")
    assert COSTS.fee(OrderSide.BUY, D("1000.5"), include_fixed=False) == D("1.000500")


def test_fee_rounds_up_sub_micro_amounts():
    c = CostModel(D(0), D("1"), D("1"), D(0))
    assert c.fee(OrderSide.BUY, D("0.01"), include_fixed=False) == D("0.000001")  # 0.000001 exactly
    assert c.fee(OrderSide.BUY, D("0.011"), include_fixed=False) == D("0.000002")  # 0.0000011 -> up


def test_zero_cost_model_is_allowed_only_when_explicit():
    zero = CostModel(D(0), D(0), D(0), D(0))
    assert zero.slipped_price(OrderSide.BUY, D("100")) == D("100")
    assert zero.fee(OrderSide.SELL, D("500"), include_fixed=True) == D("0")


def _settings(**over) -> Settings:
    base = dict(
        db_host="h", db_port=1, db_user="u", db_password="p", db_name="n", db_test_name="n_test",
        jwt_secret_key="x" * 40, access_token_expire_minutes=5,
    )
    return Settings(**{**base, **over})


def test_cost_model_is_none_until_every_value_is_configured():
    assert CostModel.from_settings(_settings()) is None
    assert CostModel.from_settings(_settings(exchange_fee_fixed_per_order="0", exchange_fee_buy_bps="1")) is None
    full = CostModel.from_settings(
        _settings(
            exchange_fee_fixed_per_order="20", exchange_fee_buy_bps="1", exchange_fee_sell_bps="2",
            exchange_slippage_bps="3",
        )
    )
    assert full == CostModel(D("20"), D("1"), D("2"), D("3"), None)


def test_execution_defaults_fail_closed():
    s = _settings()
    assert s.execution_price_source == "disabled"
    assert s.execution_approved_price_bases == []
    assert s.execution_approved_providers == []


def test_unverified_basis_can_never_be_approved_in_configuration():
    with pytest.raises(ValueError, match="UNVERIFIED_PROVIDER_NATIVE"):
        _settings(execution_approved_price_bases=[PriceBasis.UNVERIFIED_PROVIDER_NATIVE])


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
