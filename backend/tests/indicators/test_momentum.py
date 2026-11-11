"""RSI (Wilder) reference vector and edge cases."""

import pytest

from app.indicators import RSI, run
from tests.indicators import F, V_CLOSES, assert_series


def test_rsi3_reference_vector():
    # t=3: AG = 2/3, AL = 1/3 -> RS 2 -> 200/3;  t=4: AG = 10/9, AL = 2/9 -> RS 5 -> 250/3
    expected = [None, None, None, F(200, 3), F(250, 3), F(2900, 33), F(5800, 93), F(23200, 777)]
    assert_series(run(RSI(3), V_CLOSES), expected)


def test_strictly_rising_closes_give_100():
    assert run(RSI(3), [float(i) for i in range(1, 9)]) == [None] * 3 + [100.0] * 5


def test_strictly_falling_closes_give_0():
    assert run(RSI(3), [float(i) for i in range(9, 1, -1)]) == [None] * 3 + [0.0] * 5


def test_flat_closes_give_50():
    assert run(RSI(3), [7.0] * 6) == [None] * 3 + [50.0] * 3


def test_a_flat_stretch_after_gains_keeps_the_last_regime_until_losses_appear():
    out = run(RSI(2), [1.0, 2.0, 3.0, 3.0, 3.0])
    assert out == [None, None, 100.0, 100.0, 100.0]  # AL stays 0 while AG decays but stays > 0


@pytest.mark.parametrize("n", [1, 3, 14])
def test_warm_up_is_n_bars(n):
    out = run(RSI(n), [float(i % 5 + 1) for i in range(30)])
    assert out[:n] == [None] * n and all(v is not None for v in out[n:])


def test_rsi_is_bounded():
    out = run(RSI(3), V_CLOSES)
    assert all(0.0 <= v <= 100.0 for v in out if v is not None)


@pytest.mark.parametrize("bad", [0, -3, 1.5, True])
def test_invalid_period(bad):
    with pytest.raises(ValueError):
        RSI(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None])
def test_invalid_close(bad):
    with pytest.raises(ValueError):
        RSI(3).update(bad)
