"""The rules every strategy shares, tested through a minimal test-only strategy whose entry condition is always met
once a bar has been seen (so each common gate is isolated)."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, time, timedelta

import pytest

from app.indicators import IndicatorBar
from app.strategies import FLAT, Action, Family, PositionContext, ProtectiveLevels, Reason, Signal, Strategy, StrategyConfig
from tests.strategies import DAY, NEXT_DAY, at, hhmm, pbar, slot_of


class AlwaysEnter(Strategy):
    """Test double: enters whenever the common gates allow; exits only through the common square-off."""

    strategy_id = "TEST_ALWAYS"
    family = Family.TREND

    def update_indicators(self, bar, new_session):
        return {"close": bar.close}

    def entry(self, bar, values):
        return Reason.EMA_CROSS_UP, ProtectiveLevels(stop_distance=1.0), True


def bars_between(start: str, end: str, day=DAY):
    return [pbar(day, s, 100.0) for s in range(slot_of(start), slot_of(end) + 1)]


OPEN = PositionContext(is_open=True)
CLOSED = PositionContext(closed_this_bar=True)

# ---- entry cutoff and square-off -----------------------------------------------------------------------------------------


def test_entry_allowed_on_the_bar_ending_at_the_cutoff_but_not_after():
    s = AlwaysEnter()
    out = {hhmm(sig): sig for sig in (s.evaluate(b, FLAT) for b in bars_between("09:15", "15:25"))}
    assert out["14:40"].action is Action.ENTER_LONG  # ends 14:45 = cutoff
    assert out["14:45"].action is Action.NONE and out["14:45"].reason is Reason.AFTER_ENTRY_CUTOFF
    assert all(out[t].reason is Reason.AFTER_ENTRY_CUTOFF for t in ("15:00", "15:10", "15:25"))


def held(strategy, bars):
    """Evaluate with the position open from the second bar on (a session's first bar can never hold one)."""
    return [strategy.evaluate(b, FLAT if i == 0 else OPEN) for i, b in enumerate(bars)]


def test_square_off_on_the_bar_ending_at_15_15_and_after_it():
    s = AlwaysEnter(StrategyConfig(entry_cutoff=time(9, 15)))  # no entry on the first bar
    out = held(s, bars_between("09:15", "15:25"))
    by_time = {hhmm(sig): sig for sig in out}
    assert by_time["15:05"].reason is Reason.POSITION_OPEN_NO_EXIT  # ends 15:10
    assert all(by_time[t].action is Action.EXIT_LONG and by_time[t].reason is Reason.SESSION_SQUARE_OFF for t in ("15:10", "15:15", "15:20", "15:25"))


def test_square_off_when_the_intended_bar_is_missing_uses_the_next_available_bar():
    s = AlwaysEnter()
    held(s, bars_between("09:15", "15:00"))
    late = pbar(DAY, slot_of("15:20"), 100.0)  # 15:05-15:15 missing
    sig = s.evaluate(late, OPEN)
    assert (sig.action, sig.reason) == (Action.EXIT_LONG, Reason.SESSION_SQUARE_OFF)


def test_a_position_can_never_be_carried_into_the_next_session():
    s = AlwaysEnter()
    s.evaluate(pbar(DAY, 0, 100.0), FLAT)
    with pytest.raises(ValueError, match="new session"):
        s.evaluate(pbar(NEXT_DAY, 0, 100.0), OPEN)


# ---- cooldown, open positions, gaps ------------------------------------------------------------------------------------------


def test_one_bar_cooldown_after_a_position_closes():
    s = AlwaysEnter()
    bars = bars_between("09:15", "09:30")
    assert s.evaluate(bars[0], FLAT).action is Action.ENTER_LONG
    assert s.evaluate(bars[1], CLOSED).reason is Reason.COOLDOWN  # closed on this bar: no entry at its close
    assert s.evaluate(bars[2], FLAT).action is Action.ENTER_LONG  # one bar later: allowed again


def test_zero_cooldown_is_configurable():
    s = AlwaysEnter(StrategyConfig(cooldown_bars=0))
    assert s.evaluate(pbar(DAY, 0, 100.0), CLOSED).action is Action.ENTER_LONG


def test_no_duplicate_entry_while_a_position_is_open():
    s = AlwaysEnter()
    out = held(s, bars_between("09:15", "09:45"))
    assert out[0].action is Action.ENTER_LONG
    assert {sig.action for sig in out[1:]} == {Action.NONE} and {sig.reason for sig in out[1:]} == {Reason.POSITION_OPEN_NO_EXIT}


def test_entries_are_suppressed_on_the_bar_after_a_missing_expected_bar():
    s = AlwaysEnter()
    first, after_gap, next_one = pbar(DAY, slot_of("09:15"), 100.0), pbar(DAY, slot_of("09:25"), 100.0), pbar(DAY, slot_of("09:30"), 100.0)
    assert s.evaluate(first, FLAT).action is Action.ENTER_LONG
    assert s.evaluate(after_gap, FLAT).reason is Reason.GAP_SUPPRESSED  # 09:20 is missing
    assert s.evaluate(next_one, FLAT).action is Action.ENTER_LONG


def test_a_session_starting_after_its_first_expected_slot_is_a_gap():
    s = AlwaysEnter()
    assert s.evaluate(pbar(DAY, slot_of("09:25"), 100.0), FLAT).reason is Reason.GAP_SUPPRESSED
    t = AlwaysEnter()
    assert t.evaluate(pbar(DAY, 0, 100.0), FLAT).action is Action.ENTER_LONG


def test_missing_closing_bars_of_one_session_are_not_a_gap_for_the_next_session():
    s = AlwaysEnter()
    s.evaluate(pbar(DAY, slot_of("15:00"), 100.0), FLAT)  # the session's last bars are missing
    assert s.evaluate(pbar(NEXT_DAY, 0, 100.0), FLAT).action is Action.ENTER_LONG


def test_exits_are_not_suppressed_by_a_gap():
    s = AlwaysEnter()
    held(s, [pbar(DAY, slot_of("14:55"), 100.0), pbar(DAY, slot_of("15:00"), 100.0)])
    sig = s.evaluate(pbar(DAY, slot_of("15:15"), 100.0), OPEN)  # 15:05 and 15:10 missing
    assert sig.action is Action.EXIT_LONG


def test_gate_precedence_is_deterministic():
    # a bar that is both after the cutoff and after a gap reports the cutoff (it is checked first)
    s = AlwaysEnter()
    s.evaluate(pbar(DAY, slot_of("14:40"), 100.0), FLAT)
    assert s.evaluate(pbar(DAY, slot_of("14:55"), 100.0), FLAT).reason is Reason.AFTER_ENTRY_CUTOFF


# ---- input validation --------------------------------------------------------------------------------------------------------


def test_bars_must_be_on_an_expected_slot_and_in_order():
    s = AlwaysEnter()
    with pytest.raises(ValueError, match="expected 5-minute slot"):
        s.evaluate(at(DAY, "09:17", 100, 100.5, 99.5, 100), FLAT)
    s.evaluate(pbar(DAY, 5, 100.0), FLAT)
    with pytest.raises(ValueError, match="strictly increasing"):
        s.evaluate(pbar(DAY, 4, 100.0), FLAT)
    saturday = datetime(2026, 11, 7, 4, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="trading session"):
        AlwaysEnter().evaluate(IndicatorBar(saturday, saturday.date(), 100, 101, 99, 100, 1), FLAT)


def test_position_context_validation():
    with pytest.raises(ValueError):
        PositionContext(is_open=True, closed_this_bar=True)
    with pytest.raises(ValueError):
        PositionContext(is_open=1)
    with pytest.raises(ValueError, match="PositionContext"):
        AlwaysEnter().evaluate(pbar(DAY, 0, 100.0), None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entry_cutoff": time(14, 47)},  # off the 5-minute grid
        {"entry_cutoff": time(8, 0)},  # outside the session
        {"square_off": time(15, 35)},
        {"entry_cutoff": time(15, 20), "square_off": time(15, 15)},  # cutoff after square-off
        {"entry_cutoff": "14:45"},
        {"entry_cutoff": time(14, 45, tzinfo=UTC)},
        {"cooldown_bars": -1},
        {"cooldown_bars": True},
        {"cooldown_bars": 1.0},
    ],
)
def test_invalid_common_configuration(kwargs):
    with pytest.raises(ValueError):
        StrategyConfig(**kwargs)


def test_configuration_is_immutable_and_must_match_the_strategy():
    cfg = StrategyConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.cooldown_bars = 3
    from app.strategies import ORBConfig

    with pytest.raises(ValueError, match="needs a StrategyConfig"):
        AlwaysEnter(ORBConfig())


def test_protective_levels_validation():
    with pytest.raises(ValueError):
        ProtectiveLevels(stop_price=1.0, stop_distance=1.0)
    for bad in (0, -1.0, float("nan"), True):
        with pytest.raises(ValueError):
            ProtectiveLevels(stop_distance=bad)


def test_signals_are_immutable_and_only_entries_carry_protective_levels():
    sig = AlwaysEnter().evaluate(pbar(DAY, 0, 100.0), FLAT)
    assert sig.reference_price == 100.0 and sig.protective == ProtectiveLevels(stop_distance=1.0)
    with pytest.raises(FrozenInstanceError):
        sig.action = Action.NONE
    with pytest.raises(TypeError):
        sig.values["close"] = 1.0  # read-only snapshot
    with pytest.raises(ValueError):
        Signal("X", "1", sig.bar_start, Action.EXIT_LONG, Reason.SESSION_SQUARE_OFF, 1.0, ProtectiveLevels(stop_distance=1.0))


def test_there_is_no_short_action():
    assert {a.value for a in Action} == {"ENTER_LONG", "EXIT_LONG", "NONE"}
