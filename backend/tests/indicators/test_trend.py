"""DMI/ADX and Supertrend reference vectors and edge cases."""

import pytest

from app.indicators import ADX, Direction, Supertrend, run
from tests.indicators import DAY, F, assert_series, bar, close_enough, dataset_v

# ---- ADX ----------------------------------------------------------------------------------------------------------------


def test_adx3_reference_vector():
    out = run(ADX(3), dataset_v())
    assert out[:3] == [None, None, None]
    assert_series([v.plus_di for v in out[3:]], [F(100, 3), F(1000, 21), F(145, 3), F(100, 3), F(11600, 591)])
    assert_series([v.minus_di for v in out[3:]], [F(50, 3), F(200, 21), F(20, 3), F(1750, 87), F(23200, 591)])
    assert_series([v.dx for v in out[3:]], [F(100, 3), F(200, 3), F(2500, 33), F(2300, 93), F(100, 3)])
    assert_series([v.adx for v in out[3:]], [None, None, F(5800, 99), F(435500, 9207), F(1177900, 27621)])


def test_adx14_needs_28_bars():
    bars = [bar(DAY, i, 100 + i % 7, 102 + i % 7, 99 + i % 7, 101 + i % 7) for i in range(40)]
    out = run(ADX(14), bars)
    assert out[:14] == [None] * 14
    assert all(v is not None and v.adx is None for v in out[14:27])
    assert all(v.adx is not None for v in out[27:])


def test_equal_up_and_down_moves_give_no_directional_movement():
    # bar 1 expands one point both ways: up = down = 1 -> +DM = -DM = 0; bar 2 is an inside bar (up, down < 0)
    bars = [bar(DAY, 0, 10, 11, 9, 10), bar(DAY, 1, 10, 12, 8, 10), bar(DAY, 2, 10, 11, 9, 10)]
    v = run(ADX(2), bars)[2]
    assert (v.plus_di, v.minus_di, v.dx) == (0.0, 0.0, 0.0)


def test_flat_market_has_zero_di_dx_and_adx():
    out = run(ADX(2), [bar(DAY, i, 10, 10, 10, 10) for i in range(6)])
    assert all((v.plus_di, v.minus_di, v.dx) == (0.0, 0.0, 0.0) for v in out[2:])
    assert [v.adx for v in out[2:]] == [None, 0.0, 0.0, 0.0]


@pytest.mark.parametrize("bad", [0, -2, 3.0, True])
def test_adx_invalid_period(bad):
    with pytest.raises(ValueError):
        ADX(bad)


# ---- Supertrend -------------------------------------------------------------------------------------------------------


def test_supertrend_3_1_reference_vector():
    out = run(Supertrend(3, 1), dataset_v())
    assert out[:3] == [None, None, None]
    rows = [
        # basic upper, basic lower, final upper, final lower, direction, line, flipped
        (13, 9, 13, 9, Direction.DOWN, 13, False),  # initialization: DOWN by convention, never a flip
        (F(89, 6), F(61, 6), 13, F(61, 6), Direction.DOWN, 13, False),  # C = 13 is not > FU = 13 (strict)
        (F(146, 9), F(106, 9), 13, F(106, 9), Direction.UP, F(106, 9), True),  # flip up
        (F(409, 27), F(293, 27), F(409, 27), F(106, 9), Direction.UP, F(106, 9), False),  # FU resets: C_prev > FU_prev
        (F(1088, 81), F(694, 81), F(1088, 81), F(106, 9), Direction.DOWN, F(1088, 81), True),  # flip down
    ]
    for t, (value, row) in enumerate(zip(out[3:], rows), start=3):
        bu, bl, fu, fl, direction, line, flipped = row
        assert value.basic_upper == close_enough(bu) and value.basic_lower == close_enough(bl), t
        assert value.upper == close_enough(fu) and value.lower == close_enough(fl), t
        assert value.direction is direction and value.line == close_enough(line) and value.flipped is flipped, t


def test_supertrend_final_lower_band_only_rises_while_price_holds_above_it():
    out = run(Supertrend(3, 1), dataset_v())
    assert out[5].lower == out[6].lower  # BL fell (10.85 < 11.78) and C_prev 14 >= FL: the band holds


def test_supertrend_on_flat_prices_has_bands_at_the_mid():
    out = run(Supertrend(2, 3.0), [bar(DAY, i, 10, 10, 10, 10) for i in range(5)])
    assert all(v.upper == v.lower == 10.0 and v.direction is Direction.DOWN and not v.flipped for v in out[2:])


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), True])
def test_supertrend_invalid_multiplier(bad):
    with pytest.raises(ValueError):
        Supertrend(3, bad)


@pytest.mark.parametrize("bad", [0, 1.5, False])
def test_supertrend_invalid_period(bad):
    with pytest.raises(ValueError):
        Supertrend(bad, 3)
