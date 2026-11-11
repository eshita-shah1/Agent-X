"""Supertrend flip strategy, on the approved Step 1 reference dataset V with Supertrend(3, 1)."""

from datetime import time

import pytest

from app.indicators import Supertrend, run
from app.strategies import Action, Reason, SupertrendConfig, SupertrendFlip
from tests.indicators import dataset_v
from tests.strategies import DAY, events, pbar, simulate

R = Reason
SMALL = SupertrendConfig(atr_period=3, multiplier=1.0)


def test_flip_up_enters_and_flip_down_exits_on_the_reference_dataset():
    # Step 1 reference: direction DOWN from t=3, flips UP at t=5 (09:40 bar), flips DOWN at t=7 (09:50 bar)
    out = simulate(SupertrendFlip(SMALL), dataset_v())
    assert [s.reason for s in out] == [
        R.INDICATOR_NOT_READY, R.INDICATOR_NOT_READY, R.INDICATOR_NOT_READY, R.NO_ENTRY_CONDITION, R.NO_ENTRY_CONDITION,
        R.SUPERTREND_FLIP_UP, R.POSITION_OPEN_NO_EXIT, R.SUPERTREND_FLIP_DOWN,
    ]
    assert out[5].action is Action.ENTER_LONG and out[5].protective is None and out[5].reference_price == 14.0
    assert out[7].action is Action.EXIT_LONG and out[7].values["supertrend_line"] == pytest.approx(1088 / 81)


def test_the_initialization_bar_is_never_an_entry():
    out = simulate(SupertrendFlip(SMALL), dataset_v())
    assert out[3].values["direction"] == "DOWN" and out[3].values["flipped"] is False and out[3].action is Action.NONE


def test_a_flip_on_a_gated_bar_is_not_acted_on_later():
    late_cutoff = SupertrendConfig(atr_period=3, multiplier=1.0, entry_cutoff=time(9, 40))  # flip bar ends 09:45
    out = simulate(SupertrendFlip(late_cutoff), dataset_v())
    assert out[5].reason is R.AFTER_ENTRY_CUTOFF and all(s.action is Action.NONE for s in out)


def test_flip_down_without_a_position_is_not_an_entry():
    out = simulate(SupertrendFlip(SMALL), dataset_v()[:5] + dataset_v()[6:])  # drop the 09:40 flip-up bar
    assert all(s.action is Action.NONE for s in out)


def test_values_come_from_the_approved_indicator():
    out = simulate(SupertrendFlip(SMALL), dataset_v())
    direct = run(Supertrend(3, 1.0), dataset_v())
    assert [s.values["supertrend_line"] for s in out] == [None if v is None else v.line for v in direct]
    assert [s.values["flipped"] for s in out] == [None if v is None else v.flipped for v in direct]


def test_default_warm_up_is_atr_10():
    out = simulate(SupertrendFlip(), [pbar(DAY, i, 100.0 + (i % 5)) for i in range(15)])
    assert all(s.reason is R.INDICATOR_NOT_READY for s in out[:10]) and out[10].reason is not R.INDICATOR_NOT_READY


@pytest.mark.parametrize("kwargs", [{"atr_period": 0}, {"multiplier": 0}, {"multiplier": -3.0}, {"multiplier": True}])
def test_invalid_supertrend_configuration(kwargs):
    with pytest.raises(ValueError):
        SupertrendConfig(**kwargs)


def test_only_long_events():
    assert {a for _, a, _ in events(simulate(SupertrendFlip(SMALL), dataset_v()))} == {Action.ENTER_LONG, Action.EXIT_LONG}
