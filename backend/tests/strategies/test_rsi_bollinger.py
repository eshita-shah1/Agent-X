"""RSI + Bollinger mean reversion. Hand-computed sequences use RSI(2), BB(3, 1.0), ATR(2); defaults tested separately."""

import pytest

from app.indicators import ATR, RSI, Bollinger, run
from app.strategies import Action, Reason, RSIBollingerConfig, RSIBollingerReversion
from tests.strategies import DAY, events, pbar, simulate

R = Reason
SMALL = RSIBollingerConfig(rsi_period=2, bollinger_period=3, bollinger_k=1.0, atr_period=2)

# closes   10   10.5   10   10.5   9         9.5      10
# RSI2     -    -      50   75     18.75     -        -
# BB3 mid  -    -      10.17 10.33 9.833     9.667    9.5
# BB3 low  -    -                  9.2097 (59/6 - sqrt(7/18))
# t=4: close 9 < lower 9.2097 and RSI 18.75 < 30: ENTER (stop 1.5 x ATR2)
# t=5: close 9.5 < mid 9.667: hold;  t=6: close 10 >= mid 9.5: EXIT (reverted to the middle band)
CLOSES = [10, 10.5, 10, 10.5, 9, 9.5, 10]


def bars(closes=CLOSES):
    return [pbar(DAY, i, float(c)) for i, c in enumerate(closes)]


def test_oversold_below_the_lower_band_enters_and_the_middle_band_exits():
    out = simulate(RSIBollingerReversion(SMALL), bars())
    assert [s.reason for s in out] == [
        R.INDICATOR_NOT_READY, R.INDICATOR_NOT_READY, R.NO_ENTRY_CONDITION, R.NO_ENTRY_CONDITION,
        R.OVERSOLD_BELOW_LOWER_BAND, R.POSITION_OPEN_NO_EXIT, R.REVERTED_TO_MIDDLE_BAND,
    ]
    entry = out[4]
    assert entry.values["rsi"] == pytest.approx(18.75) and entry.values["bb_lower"] == pytest.approx(59 / 6 - (7 / 18) ** 0.5)
    assert entry.protective.stop_distance == pytest.approx(1.5 * entry.values["atr"])
    assert out[6].action is Action.EXIT_LONG and out[6].values["bb_mid"] == pytest.approx(9.5)


def test_both_conditions_are_required_and_strict():
    rsi_at_entry = simulate(RSIBollingerReversion(SMALL), bars())[4].values["rsi"]
    # oversold threshold equal to the RSI: 'RSI < oversold' fails
    equal = RSIBollingerConfig(rsi_period=2, bollinger_period=3, bollinger_k=1.0, atr_period=2, oversold=rsi_at_entry)
    assert simulate(RSIBollingerReversion(equal), bars())[4].reason is R.NO_ENTRY_CONDITION
    # a wider band: the close is no longer below it although RSI is oversold
    wide = RSIBollingerConfig(rsi_period=2, bollinger_period=3, bollinger_k=3.0, atr_period=2)
    out = simulate(RSIBollingerReversion(wide), bars())[4]
    assert out.values["rsi"] < 30 and out.values["close"] >= out.values["bb_lower"] and out.reason is R.NO_ENTRY_CONDITION


def test_overbought_is_validated_but_does_not_trigger_anything_while_long_only():
    out = simulate(RSIBollingerReversion(SMALL), bars([10, 11, 12, 13, 14, 15]))  # RSI 100, far above 70
    assert {s.action for s in out} == {Action.NONE}


def test_values_come_from_the_approved_indicators():
    out = simulate(RSIBollingerReversion(SMALL), bars())
    closes = [float(c) for c in CLOSES]
    assert [s.values["rsi"] for s in out] == run(RSI(2), closes)
    assert [s.values["bb_lower"] for s in out] == [None if v is None else v.lower for v in run(Bollinger(3, 1.0), closes)]
    assert [s.values["atr"] for s in out] == run(ATR(2), bars())


def test_default_warm_up_is_the_bollinger_20_and_rsi_14_and_atr_14():
    out = simulate(RSIBollingerReversion(), [pbar(DAY, i, 100.0 + (i % 3)) for i in range(25)])
    assert all(s.reason is R.INDICATOR_NOT_READY for s in out[:19])  # BB(20) needs 20 bars
    assert out[19].reason is not R.INDICATOR_NOT_READY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"oversold": 70, "overbought": 70},
        {"oversold": 0},
        {"overbought": 100},
        {"oversold": 80, "overbought": 70},
        {"bollinger_k": 0},
        {"bollinger_period": 0},
        {"rsi_period": True},
        {"stop_atr_multiple": -1},
    ],
)
def test_invalid_rsi_bollinger_configuration(kwargs):
    with pytest.raises(ValueError):
        RSIBollingerConfig(**kwargs)


def test_only_long_events():
    assert {a for _, a, _ in events(simulate(RSIBollingerReversion(SMALL), bars()))} == {Action.ENTER_LONG, Action.EXIT_LONG}
