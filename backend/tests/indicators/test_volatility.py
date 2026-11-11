"""True Range, ATR and Bollinger Bands reference vectors and edge cases."""

import pytest

from app.indicators import ATR, Bollinger, BollingerValue, TrueRange, run
from tests.indicators import DAY, F, V_CLOSES, assert_series, bar, close_enough, dataset_v


def test_true_range_reference_vector_is_undefined_on_the_first_bar():
    assert run(TrueRange(), dataset_v()) == [None, 2.0, 2.0, 2.0, 3.0, 2.0, 2.0, 3.0]


def test_atr3_reference_vector():
    assert_series(run(ATR(3), dataset_v()), [None, None, None, 2, F(7, 3), F(20, 9), F(58, 27), F(197, 81)])


def test_bollinger_3_2_reference_vector():
    mids = [None, None, 11, F(34, 3), 12, F(38, 3), F(40, 3), F(25, 2)]
    variances = [None, None, F(2, 3), F(2, 9), F(2, 3), F(14, 9), F(2, 9), F(13, 6)]
    out = run(Bollinger(3, 2), V_CLOSES)
    for t, (value, mid, var) in enumerate(zip(out, mids, variances)):
        if mid is None:
            assert value is None
            continue
        sigma = float(var) ** 0.5
        assert value.mid == close_enough(mid), t
        assert value.upper == close_enough(float(mid) + 2 * sigma), t
        assert value.lower == close_enough(float(mid) - 2 * sigma), t
    # full precision: 11 + 2 * sqrt(2/3) and 25/2 - 2 * sqrt(13/6)
    assert out[2].upper == close_enough(12.632993161855452) and out[7].lower == close_enough(9.556079711224051)


def test_constant_prices_have_zero_true_range_and_collapsed_bands():
    bars = [bar(DAY, i, 10, 10, 10, 10) for i in range(6)]
    assert run(TrueRange(), bars) == [None] + [0.0] * 5
    assert run(ATR(3), bars) == [None] * 3 + [0.0] * 3
    assert run(Bollinger(3, 2), [10.0] * 4)[2:] == [BollingerValue(10.0, 10.0, 10.0)] * 2


def test_true_range_uses_the_previous_close_across_a_gap():
    bars = [bar(DAY, 0, 10, 11, 9, 10), bar(DAY, 5, 14, 15, 14, 15)]  # four missing slots in between
    assert run(TrueRange(), bars) == [None, 5.0]  # |15 - 10|: no filling, the previous available close is used


def test_zero_k_gives_bands_equal_to_the_mid():
    v = run(Bollinger(3, 0), V_CLOSES)[2]
    assert v.upper == v.mid == v.lower == 11.0


@pytest.mark.parametrize("bad_k", [-0.1, float("nan"), float("inf"), True, None])
def test_invalid_bollinger_k(bad_k):
    with pytest.raises(ValueError):
        Bollinger(3, bad_k)


@pytest.mark.parametrize("bad", [0, 2.0, False])
def test_invalid_periods(bad):
    for make in (lambda: ATR(bad), lambda: Bollinger(bad, 2)):
        with pytest.raises(ValueError):
            make()


def test_bars_out_of_order_are_rejected():
    tr = TrueRange()
    tr.update(bar(DAY, 2, 10, 11, 9, 10))
    with pytest.raises(ValueError, match="strictly increasing"):
        tr.update(bar(DAY, 1, 10, 11, 9, 10))
