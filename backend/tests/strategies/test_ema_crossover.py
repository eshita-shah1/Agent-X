"""EMA crossover. Hand-computed sequences use EMA(2)/EMA(3)/ATR(2); the approved defaults are tested separately."""

import pytest

from app.indicators import ATR, EMA, run
from app.strategies import Action, EMACrossover, EMACrossoverConfig, Reason
from tests.strategies import DAY, events, pbar, simulate

R = Reason
SMALL = EMACrossoverConfig(fast_period=2, slow_period=3, atr_period=2)

# closes      10   10   10   9        12        12.5      10
# EMA2 (a=2/3) -   10   10   28/3     100/9     325/27    865/81
# EMA3 (a=1/2) -   -    10   9.5      10.75     11.625    10.8125
# ATR2         -   -    1    1.25     2.375
# t=4: fast crosses above slow (9.33 <= 9.5 -> 11.11 > 10.75): ENTER, stop 2 x 2.375 = 4.75
# t=6: fast crosses below slow (12.04 >= 11.625 -> 10.68 < 10.81): EXIT
CLOSES = [10, 10, 10, 9, 12, 12.5, 10]


def bars(closes=CLOSES):
    return [pbar(DAY, i, float(c)) for i, c in enumerate(closes)]


def test_cross_up_enters_and_cross_down_exits():
    out = simulate(EMACrossover(SMALL), bars())
    assert [s.reason for s in out] == [
        R.INDICATOR_NOT_READY, R.INDICATOR_NOT_READY, R.INDICATOR_NOT_READY, R.NO_ENTRY_CONDITION,
        R.EMA_CROSS_UP, R.POSITION_OPEN_NO_EXIT, R.EMA_CROSS_DOWN,
    ]
    entry = out[4]
    assert entry.action is Action.ENTER_LONG and entry.reference_price == 12.0
    assert entry.values["ema_fast"] == pytest.approx(100 / 9) and entry.values["ema_slow"] == pytest.approx(10.75)
    assert entry.protective.stop_distance == pytest.approx(4.75) and entry.protective.target_distance is None
    assert out[6].action is Action.EXIT_LONG and out[6].values["ema_fast"] == pytest.approx(865 / 81)


def test_a_cross_from_equality_counts():
    out = simulate(EMACrossover(SMALL), bars([10, 10, 10, 11]))
    assert out[3].values["prev_ema_fast"] == out[3].values["prev_ema_slow"] == 10.0 and out[3].reason is R.EMA_CROSS_UP


def test_touching_without_crossing_is_not_a_signal():
    # fast falls back to exactly the slow value: neither a cross up nor a cross down
    s = EMACrossover(SMALL)
    out = simulate(s, bars([10, 10, 10, 10, 10]))
    assert {x.reason for x in out[3:]} == {R.NO_ENTRY_CONDITION}


def test_values_come_from_the_approved_indicators():
    out = simulate(EMACrossover(SMALL), bars())
    assert [s.values["ema_fast"] for s in out] == run(EMA(2), [float(c) for c in CLOSES])
    assert [s.values["ema_slow"] for s in out] == run(EMA(3), [float(c) for c in CLOSES])
    assert [s.values["atr"] for s in out] == run(ATR(2), bars())


def test_default_warm_up_needs_22_bars():
    # EMA(21) is defined on bar 21 (index 20); a cross also needs the previous slow value -> first decision at index 21
    out = simulate(EMACrossover(), [pbar(DAY, i, 100.0 + (i % 4)) for i in range(30)])
    assert all(s.reason is R.INDICATOR_NOT_READY for s in out[:21])
    assert out[21].reason is not R.INDICATOR_NOT_READY


def test_default_stop_is_two_atr():
    closes = [100.0] * 25 + [99.0, 98.0] + [104.0]  # long flat stretch, a dip, then a jump: fast crosses above slow
    out = simulate(EMACrossover(), [pbar(DAY, i, c) for i, c in enumerate(closes)])
    entry = next(s for s in out if s.action is Action.ENTER_LONG)
    assert entry.protective.stop_distance == pytest.approx(2.0 * entry.values["atr"])


@pytest.mark.parametrize(
    "kwargs",
    [{"fast_period": 21, "slow_period": 21}, {"fast_period": 30}, {"fast_period": 0}, {"slow_period": 2.5}, {"atr_period": -1}, {"stop_atr_multiple": 0}],
)
def test_invalid_ema_configuration(kwargs):
    with pytest.raises(ValueError):
        EMACrossoverConfig(**kwargs)


def test_only_long_events():
    assert {a for _, a, _ in events(simulate(EMACrossover(SMALL), bars()))} == {Action.ENTER_LONG, Action.EXIT_LONG}
