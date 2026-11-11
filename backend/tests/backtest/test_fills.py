"""NEXT_BAR_OPEN_V1 entries and exits, stops and targets, precedence, expiry and the D5 square-off, by hand.

Every scenario evaluates NEXT_DAY with no warm-up, the pinned scripted strategy, capital 100000 and zero costs
unless stated. Flat bars sit at 100 except where overridden.
"""

from app.backtest import EventType, ExitReason, ExitTiming
from tests.backtest import D, NEXT_DAY, bar, levels, local, run, session


def only_trade(result):
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


def events(result, kind):
    return [e for e in result.events if e.event_type is kind]


# ---- entries and strategy exits -----------------------------------------------------------------------------------


def test_entry_and_strategy_exit_fill_at_the_next_bars_open():
    bars = session(NEXT_DAY, overrides={"10:05": (101,), "11:05": (102,)})
    t = only_trade(run(bars, {"10:00": None}, exits={"11:00"}))
    assert (local(t.entry_signal_bar_start), local(t.entry_bar_start), t.entry_reference_price, t.entry_price) == ("10:00", "10:05", D(100), D(101))
    assert t.quantity == 990  # floor(100000 / 101)
    assert (local(t.exit_signal_bar_start), t.exit_signal_reason, t.exit_reference_price) == ("11:00", "VWAP_CROSS_DOWN", D(100))
    assert (local(t.exit_bar_start), t.exit_timing, t.exit_reason, t.exit_price) == ("11:05", ExitTiming.AT_OPEN, ExitReason.STRATEGY_EXIT, D(102))
    assert t.gross_pnl == t.net_pnl == D(990)
    assert (t.entry_signal_reason, t.config_id, t.strategy_id, t.strategy_version) == ("ORB_BREAKOUT", "s-1", "ORB", "1")


def test_signal_reference_price_is_kept_apart_from_the_simulated_price():
    bars = session(NEXT_DAY, overrides={"10:00": (100, 100.4, 99.9, 100.3), "10:05": (100.7, 100.9, 100.6, 100.8)})
    t = only_trade(run(bars, {"10:00": None}))
    assert (t.entry_reference_price, t.entry_price) == (D("100.3"), D("100.7"))


def test_a_signal_on_the_last_bar_before_the_checkpoint_is_not_permitted_but_the_0925_bar_is():
    early = run(session(NEXT_DAY), {"09:20": None})
    assert early.trades == () and early.summary.entry_signals_ignored == 1
    t = only_trade(run(session(NEXT_DAY), {"09:25": None}))  # decision locked after 09:25 closes, then the bar is evaluated
    assert local(t.entry_bar_start) == "09:30"
    assert len(events(run(session(NEXT_DAY), {"09:25": None}), EventType.ENTRY_SIGNAL)) == 1


def test_whole_shares_without_leverage_and_insufficient_capital():
    t = only_trade(run(session(NEXT_DAY, price="333"), {"10:00": None}))
    assert t.quantity == 300 and t.entry_price * 300 <= D(100000)  # floor(100000 / 333) = 300
    r = run(session(NEXT_DAY), {"10:00": None}, capital="50")
    assert r.trades == () and len(events(r, EventType.ENTRY_SKIPPED_INSUFFICIENT_CAPITAL)) == 1


# ---- protective stops and targets ---------------------------------------------------------------------------------

STOP_1_TARGET_2 = levels(stop_distance=1.0, target_distance=2.0)  # entry at 100 -> stop 99, target 102


def protected(override_1030, extra=None, exits=()):
    overrides = {"10:30": override_1030, **(extra or {})}
    return only_trade(run(session(NEXT_DAY, overrides=overrides), {"10:00": STOP_1_TARGET_2}, exits=exits))


def test_levels_are_anchored_to_the_simulated_entry_price():
    t = protected((100,))
    assert (t.entry_price, t.stop_level, t.target_level) == (D(100), D(99), D(102))


def test_stop_touched_intrabar_exits_at_the_stop():
    t = protected((100, 100.5, 98.5, 99.5))
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_timing, t.exit_price, t.gross_pnl) == ("10:30", ExitReason.STOP, ExitTiming.INTRABAR, D(99), D(-1000))
    assert t.exit_signal_bar_start is None and t.exit_reference_price is None


def test_stop_gap_exits_at_the_open():
    t = protected((98, 98.5, 97.5, 98))
    assert (t.exit_reason, t.exit_timing, t.exit_price) == (ExitReason.STOP_GAP, ExitTiming.AT_OPEN, D(98))


def test_target_touched_intrabar_exits_at_the_target():
    t = protected((100, 102.5, 100, 101))
    assert (t.exit_reason, t.exit_timing, t.exit_price) == (ExitReason.TARGET, ExitTiming.INTRABAR, D(102))


def test_target_gap_exits_at_the_target_not_the_better_open():
    t = protected((103, 103.5, 102.8, 103))
    assert (t.exit_reason, t.exit_price) == (ExitReason.TARGET, D(102))


def test_stop_wins_when_stop_and_target_are_both_touched():
    t = protected((100, 103, 98, 100))
    assert (t.exit_reason, t.exit_price) == (ExitReason.STOP, D(99))


def test_stops_are_checked_on_the_entry_bar_after_the_entry():
    t = protected((100,), extra={"10:05": (100, 100, 98.5, 99)})
    assert (local(t.entry_bar_start), local(t.exit_bar_start), t.exit_reason, t.exit_price) == ("10:05", "10:05", ExitReason.STOP, D(99))


def test_an_absolute_stop_at_or_above_the_entry_open_gaps_out_at_that_open():
    t = only_trade(run(session(NEXT_DAY), {"10:00": levels(stop_price=100.5)}))
    assert t.entry_bar_start == t.exit_bar_start and (t.exit_reason, t.exit_price, t.gross_pnl) == (ExitReason.STOP_GAP, D(100), D(0))


def test_float_levels_keep_their_full_round_trip_precision():
    t = only_trade(run(session(NEXT_DAY), {"10:00": levels(stop_distance=0.1 + 0.2, target_distance=2.81)}))
    assert (t.stop_level, t.target_level) == (D("99.69999999999999996"), D("102.81"))  # 100 - Decimal("0.30000000000000004")


# ---- precedence at the open when a strategy exit is pending (correction B3) --------------------------------------


def test_stop_gap_beats_a_pending_strategy_exit():
    t = protected((98, 98.5, 97.5, 98), exits={"10:25"})
    assert (t.exit_reason, t.exit_timing, t.exit_price) == (ExitReason.STOP_GAP, ExitTiming.AT_OPEN, D(98))


def test_a_pending_strategy_exit_beats_a_target_gap():
    t = protected((103, 103.5, 102.8, 103), exits={"10:25"})
    assert (t.exit_reason, t.exit_timing, t.exit_price) == (ExitReason.STRATEGY_EXIT, ExitTiming.AT_OPEN, D(103))
    assert local(t.exit_signal_bar_start) == "10:25"


def test_a_pending_strategy_exit_beats_later_intrabar_stop_and_target():
    t = protected((100, 103, 98, 100), exits={"10:25"})
    assert (t.exit_reason, t.exit_price) == (ExitReason.STRATEGY_EXIT, D(100))


# ---- missing bars and expiry ---------------------------------------------------------------------------------------


def test_entry_expires_when_the_next_expected_bar_is_missing():
    r = run(session(NEXT_DAY, skip={"10:05"}), {"10:00": None})
    assert r.trades == ()
    (e,) = events(r, EventType.ENTRY_EXPIRED_MISSING_BAR)
    assert local(e.bar_start) == "10:00" and e.config_id == "s-1"


def test_entry_expires_at_the_session_end():
    r = run(session(NEXT_DAY, until="11:55"), {"11:55": None})
    assert r.trades == () and len(events(r, EventType.ENTRY_EXPIRED_SESSION_END)) == 1


def test_a_strategy_exit_is_deferred_to_the_next_available_open_when_the_next_bar_is_missing():
    r = run(session(NEXT_DAY, skip={"11:05", "11:10"}, overrides={"11:15": (101,)}), {"10:00": None}, exits={"11:00"})
    t = only_trade(r)
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_price) == ("11:15", ExitReason.STRATEGY_EXIT, D(101))
    assert len(events(r, EventType.EXIT_DEFERRED_MISSING_BAR)) == 1


def test_missing_bars_are_reported_and_never_filled():
    r = run(session(NEXT_DAY, skip={"11:05", "11:10"}))
    (e,) = events(r, EventType.MISSING_BARS)
    assert (local(e.bar_start), e.detail["count"], local(e.detail["first_missing_start"])) == ("11:15", 2, "11:05")
    assert len(r.equity) == 73 and r.data_quality.missing_bars == 2


# ---- session square-off (D5, correction B1) -----------------------------------------------------------------------


def test_normal_square_off_fills_at_the_1515_open():
    t = only_trade(run(session(NEXT_DAY, overrides={"15:15": (101,)}), {"14:00": None}))
    assert (local(t.exit_signal_bar_start), t.exit_signal_reason) == ("15:10", "SESSION_SQUARE_OFF")
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_timing, t.exit_price) == ("15:15", ExitReason.SESSION_SQUARE_OFF, ExitTiming.AT_OPEN, D(101))


def test_missing_square_off_bar_closes_at_the_last_available_bars_close():
    bars = session(NEXT_DAY, skip={"15:15"}, overrides={"15:10": (100, 100.5, 99.5, 100.2), "15:20": (105,)})
    r = run(bars, {"14:00": None})
    t = only_trade(r)
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_timing, t.exit_price) == ("15:10", ExitReason.SQUARE_OFF_FALLBACK_LAST_BAR, ExitTiming.AT_CLOSE, D("100.2"))
    assert t.exit_signal_reason == "SESSION_SQUARE_OFF"
    assert [p.quantity for p in r.equity if local(p.bar_start) >= "15:10"] == [0, 0, 0]  # flat from the 15:10 close


def test_a_session_whose_data_ends_early_closes_at_its_last_bar():
    r = run(session(NEXT_DAY, until="13:00", overrides={"13:00": (100, 100, 99, 99.5)}), {"10:00": None})
    t = only_trade(r)
    assert (local(t.exit_bar_start), t.exit_reason, t.exit_price, t.exit_signal_bar_start) == ("13:00", ExitReason.SQUARE_OFF_FALLBACK_LAST_BAR, D("99.5"), None)
    assert r.equity[-1].quantity == 0


def test_no_position_is_ever_carried_overnight():
    from tests.backtest import THIRD_DAY

    bars = session(NEXT_DAY, until="12:00") + session(THIRD_DAY)
    r = run(bars, {"10:00": None, (THIRD_DAY, "10:00"): None}, end=THIRD_DAY)
    assert [(t.session_date, t.exit_reason) for t in r.trades] == [(NEXT_DAY, ExitReason.SQUARE_OFF_FALLBACK_LAST_BAR), (THIRD_DAY, ExitReason.SESSION_SQUARE_OFF)]
    assert all(t.entry_bar_start.date() == t.exit_bar_start.date() for t in r.trades)


# ---- cooldown wiring ----------------------------------------------------------------------------------------------


def test_a_stop_starts_the_strategys_cooldown():
    bars = session(NEXT_DAY, overrides={"10:30": (100, 100.5, 98.5, 99.5)})
    r = run(bars, {"10:00": levels(stop_distance=1.0), "10:30": None, "10:40": None})
    assert [(local(t.entry_signal_bar_start), t.exit_reason) for t in r.trades] == [("10:00", ExitReason.STOP), ("10:40", ExitReason.SESSION_SQUARE_OFF)]


# ---- costs and slippage -------------------------------------------------------------------------------------------


def test_costs_and_slippage_by_hand():
    from app.backtest import CostBasis, CostModel

    costs = CostModel(fee_per_order=D(20), fee_bps_per_side=D(3), sell_side_bps=D(10), slippage_bps=D(5))
    r = run(session(NEXT_DAY, overrides={"11:05": (101,)}), {"10:00": None}, exits={"11:00"}, costs=costs)
    t = only_trade(r)
    assert (t.entry_price, t.quantity, t.entry_fees) == (D("100.05"), 999, D("49.98"))  # 1000 shares would need 100050
    assert (t.exit_price, t.exit_fees) == (D("100.9495"), D("151.10"))
    assert (t.gross_pnl, t.net_pnl) == (D("898.6005"), D("697.5205"))
    assert r.summary.final_equity == D("100697.5205") and r.summary.total_fees == D("201.08")
    assert costs.cost_basis is CostBasis.NET_OF_MODELLED_COSTS and CostModel().cost_basis is CostBasis.GROSS_OF_COSTS


# ---- equity --------------------------------------------------------------------------------------------------------


def test_equity_is_recorded_for_every_evaluation_bar_in_decimal():
    r = run(session(NEXT_DAY, overrides={"10:05": (101,), "10:10": (102,)}), {"10:00": None})
    assert len(r.equity) == 75
    for p in r.equity:
        assert p.equity == p.cash + p.quantity * p.close and type(p.equity) is D
    by_time = {local(p.bar_start): p for p in r.equity}
    assert (by_time["10:00"].quantity, by_time["10:05"].quantity, by_time["10:10"].equity) == (0, 990, D(10) + 990 * D(102))
