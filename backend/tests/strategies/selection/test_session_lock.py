"""One locked decision per instrument per session, made at the 09:30 checkpoint from bars ending at or before it."""

import pytest

from app.indicators import ADX, run
from app.strategies.selection import (
    InstrumentSelector,
    PinnedPolicy,
    Regime,
    RegimePriorityPolicy,
    SelectionBook,
    SelectionStatus,
)
from tests.strategies import hhmm, pbar, slot_of
from tests.strategies.selection import ALL, DAY, EMA, NEXT_DAY, THIRD_DAY, flat, later, mixed, trending


def feed(selector, bars):
    return [selector.observe(b) for b in bars]


def chop(day, start="09:15", end="15:25", base=100.0):
    """Alternating up/down bars: +DM and -DM alternate, DX stays low, ADX decays towards RANGE."""
    return [pbar(day, s, base + (0.5 if s % 2 else 0.0)) for s in range(slot_of(start), slot_of(end) + 1)]


def test_the_decision_is_made_on_the_bar_ending_at_09_30_and_only_once():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    out = feed(sel, trending() + later())
    made = [d for d in out if d is not None and d.session_date == NEXT_DAY]
    assert len(made) == 1 and hhmm_of(made[0].decided_on_bar_start) == "09:25"
    assert made[0] is sel.decision(NEXT_DAY)
    assert [d.session_date for d in out if d is not None] == [DAY, NEXT_DAY]  # one decision per session


def hhmm_of(ts):
    from app.marketdata.calendar import EXCHANGE_TZ

    return ts.astimezone(EXCHANGE_TZ).strftime("%H:%M")


def test_a_missing_09_25_bar_decides_on_the_next_bar_without_using_it():
    bars = trending(day2_slots=(0, 1)) + [pbar(NEXT_DAY, slot_of("09:30"), 500.0, 50.0)]  # a wild bar after 09:30
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    d = [x for x in feed(sel, bars) if x and x.session_date == NEXT_DAY][0]
    assert hhmm_of(d.decided_on_bar_start) == "09:30" and hhmm_of(d.regime.input_bar_start) == "09:20"
    assert d.regime.session_bars_used == 2 and d.regime.adx == run(ADX(14), bars[:-1])[-1].adx  # the 09:30 bar is excluded


def test_a_session_whose_first_bar_is_after_the_checkpoint_decides_from_earlier_bars_only():
    bars = trending(day2_slots=()) + [pbar(NEXT_DAY, slot_of("09:35"), 200.0)]
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    d = [x for x in feed(sel, bars) if x and x.session_date == NEXT_DAY][0]
    assert d.regime.session_bars_used == 0 and d.regime.input_bar_start.date() == DAY
    assert d.candidates[0].outcome.value == "REJECTED_WARMUP_NOT_SATISFIED" and d.selected_config_id == "ema-1"  # no opening range


def test_a_session_ending_before_its_checkpoint_has_no_decision_and_no_permitted_entries():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, trending(day2_slots=(0,)) + [pbar(THIRD_DAY, s, 140.0 + s) for s in range(3)])
    assert sel.decision(NEXT_DAY) is None and not any(sel.entry_permitted(c.config_id, NEXT_DAY) for c in ALL)
    assert sel.decision(THIRD_DAY) is not None


def test_duplicate_checkpoint_calls_return_the_same_locked_decision():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, trending())
    first = sel.decision(NEXT_DAY)
    assert sel.checkpoint(NEXT_DAY) is first and sel.checkpoint(NEXT_DAY) is first
    assert feed(sel, later()) == [None] * len(later()) and sel.decision(NEXT_DAY) is first


def test_an_explicit_checkpoint_call_is_locked_against_later_bars():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, trending(day2_slots=(0, 1)))
    early = sel.checkpoint(NEXT_DAY)  # the caller knows 09:30 has passed although the 09:25 bar has not arrived
    assert early.decided_on_bar_start is None and early.regime.session_bars_used == 2
    assert feed(sel, [pbar(NEXT_DAY, 2, 200.0)] + later()) == [None] * (1 + len(later()))
    assert sel.decision(NEXT_DAY) is early


def test_checkpoint_is_only_for_the_current_session():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    with pytest.raises(ValueError):
        sel.checkpoint(NEXT_DAY)  # nothing observed yet
    feed(sel, trending())
    with pytest.raises(ValueError):
        sel.checkpoint(THIRD_DAY)


def test_regime_changes_after_the_checkpoint_never_switch_the_selection():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, trending())
    locked = sel.decision(NEXT_DAY)
    assert locked.regime.regime is Regime.TREND and locked.selected_config_id == "orb-1"
    feed(sel, chop(NEXT_DAY, "09:30"))  # ADX collapses later in the session
    assert run(ADX(14), trending() + chop(NEXT_DAY, "09:30"))[-1].adx < 20
    assert sel.decision(NEXT_DAY) is locked
    assert sel.entry_permitted("orb-1", NEXT_DAY) and not sel.entry_permitted("rsi-1", NEXT_DAY)


def test_no_selection_is_locked_too_no_mid_session_retry():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, mixed())
    assert sel.decision(NEXT_DAY).status is SelectionStatus.NO_SELECTION
    feed(sel, [pbar(NEXT_DAY, s, 200.0 + 2 * s) for s in range(3, 40)])  # a strong trend later: ADX >= 25
    assert sel.decision(NEXT_DAY).status is SelectionStatus.NO_SELECTION
    assert not any(sel.entry_permitted(c.config_id, NEXT_DAY) for c in ALL)


def test_a_new_session_makes_a_new_decision():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    feed(sel, trending() + chop(NEXT_DAY, "09:30") + chop(THIRD_DAY, "09:15", "09:25"))
    day2, day3 = sel.decision(NEXT_DAY), sel.decision(THIRD_DAY)
    assert (day2.regime.regime, day2.selected_config_id) == (Regime.TREND, "orb-1")
    assert (day3.regime.regime, day3.selected_config_id) == (Regime.RANGE, "rsi-1")
    assert sel.entry_permitted("orb-1", NEXT_DAY) and not sel.entry_permitted("orb-1", THIRD_DAY)


def test_pinned_selection_is_locked_for_the_session_too():
    sel = InstrumentSelector("RELIANCE", PinnedPolicy(EMA))
    feed(sel, trending())
    locked = sel.decision(NEXT_DAY)
    feed(sel, chop(NEXT_DAY, "09:30"))
    assert sel.decision(NEXT_DAY) is locked and sel.entry_permitted("ema-1", NEXT_DAY)


def test_instruments_have_independent_state():
    book = SelectionBook({"RELIANCE": RegimePriorityPolicy(ALL), "TCS": RegimePriorityPolicy(ALL)})
    for b in trending():
        book.observe("RELIANCE", b)
    assert book.decision("TCS", NEXT_DAY) is None  # TCS saw nothing
    for b in flat():
        book.observe("TCS", b)
    assert book.decision("RELIANCE", NEXT_DAY).selected_config_id == "orb-1"
    assert book.decision("TCS", NEXT_DAY).selected_config_id == "rsi-1"
    assert book.entry_permitted("RELIANCE", "orb-1", NEXT_DAY) and not book.entry_permitted("TCS", "orb-1", NEXT_DAY)
    with pytest.raises(ValueError):
        book.observe("INFY", trending()[0])
    with pytest.raises(ValueError):
        SelectionBook({})


def test_bars_must_be_valid_and_ordered():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    sel.observe(pbar(DAY, 5, 100.0))
    with pytest.raises(ValueError):
        sel.observe(pbar(DAY, 4, 100.0))
    with pytest.raises(ValueError):
        InstrumentSelector("RELIANCE", "ALL")
