"""Cost model (B3) and position accounting. Rates come from tests/fixtures/cost_schedule_synthetic.json: SYNTHETIC
values for formula checks, not real NSE/SEBI/tax/broker charges. No test here claims broker compatibility."""

import copy
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.enums import OrderSide, PriceBasis, Product
from app.exchange.accounting import AccountingError, PositionState, apply_buy, apply_sell, unrealized_pnl
from app.exchange.costs import (
    ChargeType,
    CostScheduleError,
    FeeContext,
    RoundingStage,
    breakdown_component,
    load_cost_schedule,
    parse_cost_schedule,
)
from tests.cost_fixtures import FIXTURE_SCHEDULE_PATH, fixture_costs, fixture_schedule_dict, modified, schedule_from

D = Decimal
COSTS = fixture_costs()
B, S = OrderSide.BUY, OrderSide.SELL


# ---- slippage -----------------------------------------------------------------------------------------------


def test_slippage_is_adverse_for_both_sides():
    assert COSTS.slipped_price(B, D("100")) == D("100.050000")
    assert COSTS.slipped_price(S, D("110")) == D("109.945000")


def test_slipped_prices_round_against_the_trader():
    c = schedule_from(modified(**{"T-SLIP-D": {"value": "3"}})).resolve(Product.DELIVERY, date(2026, 1, 5))
    assert c.slipped_price(B, D("123.4567")) == D("123.493738")  # 123.49373701 up
    assert c.slipped_price(S, D("123.4567")) == D("123.419662")  # down


# ---- components -----------------------------------------------------------------------------------------------


def test_buy_fee_components():
    f = COSTS.fees(B, D("1000.5"))
    assert f.charged(ChargeType.BROKERAGE) == D("1.000500")
    assert f.charged(ChargeType.STT) == 0 and f.charged(ChargeType.SEBI_FEE) == 0 and f.charged(ChargeType.IPFT) == 0
    assert f.charged(ChargeType.TXN_CHARGE) == D("0.300150")
    assert f.charged(ChargeType.STAMP_DUTY) == D("0.300150")  # buy side only
    assert ChargeType.DP_CHARGE not in f.components  # DP charges are sell-side only
    assert f.charged(ChargeType.GST) == D("0.234117")
    assert f.total == D("1.834917")


def test_sell_fee_components_have_stt_and_no_stamp_duty():
    f = COSTS.fees(S, D("1099.45"))
    assert (f.charged(ChargeType.BROKERAGE), f.charged(ChargeType.STT), f.charged(ChargeType.TXN_CHARGE)) == (
        D("1.099450"), D("2.748625"), D("0.329835"))
    assert ChargeType.STAMP_DUTY not in f.components
    assert f.charged(ChargeType.GST) == D("0.257272")
    assert f.total == D("4.435182")


def _costs(**changes):
    return schedule_from(modified(**changes)).resolve(Product.DELIVERY, date(2026, 1, 5))


def test_dp_charge_once_per_scrip_per_sell_day_with_configurable_gst():
    c = _costs(**{"T-DP-D": {"value": "15"}})
    first = c.fees(S, D("1000"))
    assert first.charged(ChargeType.DP_CHARGE) == D("15")
    # GST base includes DP in this fixture: 18 % of (brokerage 1 + txn 0.3 + DP 15)
    assert first.charged(ChargeType.GST) == D("2.934000")
    again = c.fees(S, D("1000"), FeeContext(dp_already_charged=True))
    assert again.charged(ChargeType.DP_CHARGE) == 0
    no_gst_on_dp = _costs(**{"T-DP-D": {"value": "15"}, "T-GST-D": {"applies_to": ["BROKERAGE", "TXN_CHARGE", "SEBI_FEE", "IPFT"]}})
    assert no_gst_on_dp.fees(S, D("1000")).charged(ChargeType.GST) == D("0.234000")


def test_ipft_is_a_turnover_charge_and_can_be_in_the_gst_base():
    c = _costs(**{"T-IPFT-D": {"value": "0.1"}})
    f = c.fees(B, D("100000"))
    assert f.charged(ChargeType.IPFT) == D("1")
    assert f.charged(ChargeType.GST) == (D("20") + D("30") + D("1")) * D("0.18")  # brokerage capped at 20


def test_brokerage_modes():
    capped = COSTS
    assert capped.fees(B, D("50000")).charged(ChargeType.BROKERAGE) == D("20")
    assert capped.fees(B, D("50000"), FeeContext(brokerage_raw_on_order=D("15"))).charged(ChargeType.BROKERAGE) == D("5")
    assert capped.fees(B, D("50000"), FeeContext(brokerage_raw_on_order=D("20"))).charged(ChargeType.BROKERAGE) == 0
    flat = _costs(**{"T-BRK-D": {"basis": "FLAT_PER_ORDER", "value": "20", "cap": None}})
    assert flat.fees(B, D("10")).charged(ChargeType.BROKERAGE) == D("20")  # regardless of size
    assert flat.fees(B, D("10"), FeeContext(first_fill_of_order=False)).charged(ChargeType.BROKERAGE) == 0
    pct = _costs(**{"T-BRK-D": {"basis": "TURNOVER_BPS", "value": "10", "cap": None}})
    assert pct.fees(B, D("50000")).charged(ChargeType.BROKERAGE) == D("50")  # no cap
    zero = _costs(**{"T-BRK-D": {"basis": "FLAT_PER_ORDER", "value": "0", "cap": None}})
    assert zero.fees(B, D("50000")).charged(ChargeType.BROKERAGE) == 0  # explicit zero brokerage is allowed


# ---- effective-dated selection ------------------------------------------------------------------------------


def _with_stt_change():
    d = fixture_schedule_dict()
    for r in d["rates"]:
        if r["rate_id"] == "T-STT-DS":
            r["effective_to"] = "2026-01-05"
    d["rates"].append({"rate_id": "T-STT-DS-2", "product": "DELIVERY", "charge_type": "STT", "side": "SELL",
                       "basis": "TURNOVER_BPS", "value": "40", "effective_from": "2026-01-05",
                       "source": "synthetic test fixture (rate change)"})
    return schedule_from(d)


def test_the_rate_in_force_on_the_transaction_date_is_used():
    sched = _with_stt_change()
    before = sched.resolve(Product.DELIVERY, date(2026, 1, 4))  # effective_to is exclusive
    after = sched.resolve(Product.DELIVERY, date(2026, 1, 5))  # effective_from is inclusive
    assert before.fees(S, D("1000")).charged(ChargeType.STT) == D("2.5")
    assert after.fees(S, D("1000")).charged(ChargeType.STT) == D("4")
    assert before.rate(ChargeType.STT, S).rate_id == "T-STT-DS" and after.rate(ChargeType.STT, S).rate_id == "T-STT-DS-2"
    assert after.snapshot()["rates"]["STT:SELL"]["rate_id"] == "T-STT-DS-2"


def test_a_missing_rate_for_the_date_is_an_explicit_error():
    d = fixture_schedule_dict()
    for r in d["rates"]:
        if r["rate_id"] == "T-TXN-D":
            r["effective_to"] = "2026-01-01"
    with pytest.raises(CostScheduleError, match="no TXN_CHARGE rate for DELIVERY BUY on 2026-01-05"):
        schedule_from(d).resolve(Product.DELIVERY, date(2026, 1, 5))
    assert schedule_from(d).resolve(Product.DELIVERY, date(2025, 12, 31))  # still valid before the gap


def test_products_select_their_own_rates():
    d = fixture_schedule_dict()
    intraday = []
    for r in d["rates"]:
        if r["charge_type"] == "DP_CHARGE":
            continue
        r2 = dict(r, rate_id=r["rate_id"].replace("-D", "-I"), product="INTRADAY")
        if r2["charge_type"] == "STT" and r2["side"] == "SELL":
            r2["value"] = "2.5"
        intraday.append(r2)
    d["rates"] += intraday
    sched = schedule_from(d)
    dl = sched.resolve(Product.DELIVERY, date(2026, 1, 5))
    it = sched.resolve(Product.INTRADAY, date(2026, 1, 5))
    assert dl.fees(S, D("1000")).charged(ChargeType.STT) == D("2.5")
    assert it.fees(S, D("1000")).charged(ChargeType.STT) == D("0.25")
    assert ChargeType.DP_CHARGE not in it.fees(S, D("1000")).components  # intraday has no DP charge
    with pytest.raises(CostScheduleError, match="no STT rate for INTRADAY"):
        schedule_from(fixture_schedule_dict()).resolve(Product.INTRADAY, date(2026, 1, 5))


# ---- schedule validation ----------------------------------------------------------------------------------


@pytest.mark.parametrize("change,message", [
    ({"T-STAMP-D": {"side": "SELL"}}, "BUY side only"),
    ({"T-DP-D": {"side": "BUY"}}, "SELL side only"),
    ({"T-DP-D": {"product": "INTRADAY"}}, "DELIVERY only"),
    ({"T-GST-D": {"applies_to": []}}, "non-empty `applies_to`"),
    ({"T-GST-D": {"applies_to": ["GST"]}}, "only list base charges"),
    ({"T-BRK-D": {"cap": None}}, "`cap` is required"),
    ({"T-STT-DB": {"basis": "FLAT_PER_ORDER"}}, "not valid for STT"),
    ({"T-STT-DB": {"value": "-1"}}, "greater than or equal to 0"),
    ({"T-STT-DB": {"effective_to": "2019-01-01"}}, "effective_to must be after"),
    ({"T-STT-DB": {"source": ""}}, "at least 1 character"),
])
def test_invalid_schedules_are_refused(change, message):
    with pytest.raises(CostScheduleError, match=message):
        schedule_from(modified(**change))


def test_overlapping_rates_are_refused():
    d = fixture_schedule_dict()
    d["rates"].append(dict(d["rates"][1], rate_id="T-STT-DB-DUP", effective_from="2025-01-01"))
    with pytest.raises(CostScheduleError, match="overlap"):
        schedule_from(d)


def test_every_money_charge_needs_a_rounding_rule():
    d = fixture_schedule_dict()
    d["rounding"] = [r for r in d["rounding"] if r["charge_type"] != "IPFT"]
    with pytest.raises(CostScheduleError, match="missing"):
        schedule_from(d)


def test_an_all_zero_rate_set_is_refused_but_explicit_zero_components_are_allowed():
    d = fixture_schedule_dict()
    for r in d["rates"]:
        r["value"] = "0"
        if r.get("cap") is not None:
            r["cap"] = "0"
    with pytest.raises(CostScheduleError, match="zero costs cannot enable execution"):
        schedule_from(d).resolve(Product.DELIVERY, date(2026, 1, 5))
    assert COSTS.rate(ChargeType.STT, B).value == 0  # a single explicit zero is fine


def test_unreadable_and_malformed_files_are_explicit_errors(tmp_path):
    with pytest.raises(CostScheduleError, match="cannot read"):
        load_cost_schedule(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(CostScheduleError, match="invalid cost schedule"):
        load_cost_schedule(bad)


def test_snapshot_records_version_hash_and_rate_ids():
    snap = COSTS.snapshot()
    sched = load_cost_schedule(FIXTURE_SCHEDULE_PATH)
    assert snap["schedule_version"] == "SYNTHETIC-TEST-1" and snap["schedule_sha256"] == sched.sha256
    assert snap["product"] == "DELIVERY" and snap["rate_date"] == "2026-01-05"
    assert snap["rates"]["BROKERAGE:BUY"]["rate_id"] == "T-BRK-D"
    assert snap["rounding"]["GST"] == {"stage": "FILL", "quantum": "0.000001", "mode": "UP"}


# ---- rounding ---------------------------------------------------------------------------------------------


def _rounded(stage: str, quantum: str, mode: str, charge: str = "STT"):
    d = fixture_schedule_dict()
    for r in d["rounding"]:
        if r["charge_type"] == charge:
            r.update(stage=stage, quantum=quantum, mode=mode)
    return schedule_from(d).resolve(Product.DELIVERY, date(2026, 1, 5))


@pytest.mark.parametrize("mode,expected", [("UP", "3"), ("DOWN", "2"), ("HALF_UP", "3"), ("HALF_EVEN", "2")])
def test_rounding_modes_at_rupee_quantum(mode, expected):
    c = _rounded("FILL", "1", mode)
    assert c.fees(S, D("1000")).charged(ChargeType.STT) == D(expected)  # raw 2.5


def test_rounding_is_per_charge_type():
    c = _rounded("FILL", "1", "HALF_UP")  # only STT
    f = c.fees(S, D("1099.45"))
    assert f.charged(ChargeType.STT) == D("3")  # 2.748625 -> 3
    assert f.charged(ChargeType.TXN_CHARGE) == D("0.329835")  # untouched (6 dp)


@pytest.mark.parametrize("stage", ["ORDER", "SESSION", "SESSION_INSTRUMENT"])
def test_cumulative_rounding_matches_the_rounded_group_total(stage):
    c = _rounded(stage, "1", "HALF_UP")
    assert c.rounding_stage(ChargeType.STT) is RoundingStage(stage)
    raw_sum = charged_sum = D(0)
    for turnover in ("1000", "600", "200", "1300"):  # STT raw 2.5, 1.5, 0.5, 3.25
        f = c.fees(S, D(turnover), FeeContext(prior={ChargeType.STT: (raw_sum, charged_sum)}))
        comp = f.components[ChargeType.STT]
        assert comp.charged >= 0
        raw_sum += comp.raw
        charged_sum += comp.charged
        assert charged_sum == (raw_sum).quantize(D(1), rounding="ROUND_HALF_UP")  # contract-note total
    assert charged_sum == D("8")  # 7.75 -> 8


def test_breakdown_reader_understands_the_pre_0007_format():
    legacy = {"brokerage": "1.000500", "gst": "0.234117", "total": "1.834917"}
    assert breakdown_component(legacy, ChargeType.BROKERAGE) == (D("1.0005"), D("1.0005"))
    assert breakdown_component(legacy, ChargeType.IPFT) == (D(0), D(0))
    assert breakdown_component(COSTS.fees(B, D("1000.5")).as_dict(), ChargeType.GST)[1] == D("0.234117")


# ---- conservative bounds ----------------------------------------------------------------------------------


@pytest.mark.parametrize("brokerage", [
    {"basis": "TURNOVER_BPS_CAPPED_PER_ORDER", "value": "10", "cap": "20"},
    {"basis": "FLAT_PER_ORDER", "value": "20", "cap": None},
    {"basis": "TURNOVER_BPS", "value": "30", "cap": None},
])
@pytest.mark.parametrize("cash", ["50", "1002.335", "1021", "5000", "100000", "1234567.89"])
@pytest.mark.parametrize("price", ["100.05", "99", "2500.123456"])
def test_max_buy_quantity_never_overspends(brokerage, cash, price):
    c = _costs(**{"T-BRK-D": brokerage})
    p, cash_ = D(price), D(cash)
    q = c.max_buy_quantity(p, cash_)
    if q:
        turnover = (p * q).quantize(D("0.000001"))
        assert turnover + c.fee(B, turnover) <= cash_
        assert c.buy_cost_upper(p, q) <= cash_ or q == 0
    q2 = q + 2
    turnover2 = (p * q2).quantize(D("0.000001"))
    assert turnover2 + c.fee(B, turnover2) > cash_  # and it is close to the true maximum


def test_buy_cost_upper_bounds_the_real_cost_including_rupee_rounding():
    c = _rounded("SESSION", "1", "UP", "STAMP_DUTY")
    for q in (1, 10, 1000):
        turnover = D("100.05") * q
        assert turnover + c.fee(B, turnover) <= c.buy_cost_upper(D("100.05"), q)


# ---- configuration ----------------------------------------------------------------------------------------


def _settings(**over) -> Settings:
    base = dict(
        db_host="h", db_port=1, db_user="u", db_password="p", db_name="n", db_test_name="n_test",
        jwt_secret_key="x" * 40, access_token_expire_minutes=5,
    )
    return Settings(**{**base, **over})


_ALL_COSTS = dict(exchange_cost_schedule_path=str(FIXTURE_SCHEDULE_PATH))


def test_execution_defaults_fail_closed():
    s = _settings()
    assert s.execution_mode == "disabled"
    assert s.execution_config_problems() == []
    assert s.execution_approved_price_bases == []
    assert s.execution_approved_providers == []


def test_unverified_basis_can_never_be_approved_in_configuration():
    with pytest.raises(ValueError, match="UNVERIFIED_PROVIDER_NATIVE"):
        _settings(execution_approved_price_bases=[PriceBasis.UNVERIFIED_PROVIDER_NATIVE])


def test_the_retired_same_bar_close_source_cannot_be_configured():
    with pytest.raises(ValueError):
        _settings(execution_mode="candle_close")


# ---- execution enabling safeguards (validated at start-up) ------------------------------------------------

_SIM = dict(
    execution_mode="simulation_provider_native", execution_approval_reference="OWNER-APPROVAL-1",
    execution_approved_providers=["yahoo"], **_ALL_COSTS,
)


def test_a_complete_simulation_configuration_is_accepted():
    s = _settings(**_SIM)
    assert s.execution_config_problems() == []


@pytest.mark.parametrize(
    "change,message",
    [
        (dict(execution_approval_reference=None), "EXECUTION_APPROVAL_REFERENCE"),
        (dict(execution_approval_reference="   "), "EXECUTION_APPROVAL_REFERENCE"),
        (dict(execution_approved_providers=[]), "exactly one provider"),
        (dict(execution_approved_providers=["yahoo", "other"]), "exactly one provider"),
        (dict(execution_approved_price_bases=["AS_TRADED_VERIFIED"]), "must be empty"),
        (dict(exchange_cost_schedule_path=None), "EXCHANGE_COST_SCHEDULE_PATH"),
        (dict(exchange_cost_schedule_path="/nonexistent/schedule.json"), "cannot read cost schedule"),
        (dict(exchange_product="INTRADAY"), "only EXCHANGE_PRODUCT=DELIVERY"),
    ],
)
def test_simulation_mode_refuses_to_start_without_every_safeguard(change, message):
    with pytest.raises(ValueError, match=message):
        _settings(**{**_SIM, **change})


def test_schedule_coverage_is_checked_for_the_trading_date():
    s = _settings(**_SIM)
    assert s.execution_config_problems(date(2026, 1, 5)) == []
    problems = s.execution_config_problems(date(2019, 12, 31))  # before every fixture rate's effective_from
    assert any("no BROKERAGE rate for DELIVERY BUY on 2019-12-31" in p for p in problems)


def test_verified_mode_requires_an_approved_verified_basis():
    with pytest.raises(ValueError, match="requires at least one verified basis"):
        _settings(**{**_SIM, "execution_mode": "verified_next_bar_open"})
    ok = _settings(**{**_SIM, "execution_mode": "verified_next_bar_open",
                      "execution_approved_price_bases": ["AS_TRADED_VERIFIED"]})
    assert ok.execution_config_problems() == []


@pytest.mark.parametrize("tf", ["60m", "1d"])
def test_execution_timeframe_must_align_with_the_session_grid(tf):
    with pytest.raises(ValueError, match="aligned"):
        _settings(execution_candle_timeframe=tf)


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


def test_effective_to_is_exclusive():
    d = fixture_schedule_dict()
    for r in d["rates"]:
        if r["rate_id"] == "T-STT-DS":
            r["effective_to"] = "2026-01-05"  # no successor
    sched = schedule_from(d)
    assert sched.resolve(Product.DELIVERY, date(2026, 1, 4))
    with pytest.raises(CostScheduleError, match="no STT rate for DELIVERY SELL on 2026-01-05"):
        sched.resolve(Product.DELIVERY, date(2026, 1, 5))
