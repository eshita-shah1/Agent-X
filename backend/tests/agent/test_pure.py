"""Pure Phase 5 rules: idempotency keys, the approved signal-to-order mapping (D3, D4), position context from fills,
the lifecycle table, configuration fingerprints and the replay of all five approved strategies."""

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.agent.configuration import AgentConfigInput, next_session
from app.agent.lifecycle import ALLOWED
from app.agent.pipeline import entry_key, exit_key, protective_prices, upper_bound_quantity
from app.agent.positions import FillPoint, contexts_for_bars
from app.agent.replay import ArchivedBar, replay
from app.db.enums import AgentStatus, OrderSide
from app.indicators.core import IndicatorBar
from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.strategies.base import FLAT, Action, ProtectiveLevels, Reason, Signal
from app.strategies.configs import build_policy, corporate_action_floor, warmup_sessions_before
from app.strategies.registry import STRATEGIES
from app.strategies.selection import InstrumentSelector

D = Decimal
CAL = TradingCalendar()
DAYS = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]


def ist(day, h, m):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=EXCHANGE_TZ).astimezone(UTC)


def signal(protective):
    return Signal("ORB", "1", ist(DAYS[0], 9, 30), Action.ENTER_LONG, Reason.ORB_BREAKOUT, 102.0, protective, {})


# ---- keys and mapping ----------------------------------------------------------------------------------------------


def test_keys_are_deterministic_short_and_distinct():
    t = ist(DAYS[0], 9, 30)
    assert entry_key(7, 3, t) == entry_key(7, 3, t)
    assert entry_key(7, 3, t) != entry_key(7, 4, t) != entry_key(8, 3, t)
    assert entry_key(7, 3, t) != entry_key(7, 3, t + timedelta(minutes=5))
    assert exit_key(7, t, t) != entry_key(7, 3, t)
    assert len(entry_key(2**31, 2**62, t)) <= 64 and len(exit_key(2**31, t, t)) <= 64


def test_absolute_stops_are_used_as_given_and_distances_anchor_to_the_signal_close():
    assert protective_prices(signal(ProtectiveLevels(stop_price=99.0, target_distance=3.0)), D("102")) == (D("99"), D("105"))
    stop, target = protective_prices(signal(ProtectiveLevels(stop_distance=1.25)), D("102.40"))
    assert stop == D("101.150000") and target is None
    assert protective_prices(signal(None), D("102")) == (None, None)


def test_the_proposal_is_an_upper_bound_never_a_risk_size():
    assert upper_bound_quantity(None, D("10000"), D("101"), None) == 99
    assert upper_bound_quantity(500, D("1"), D("101"), None) == 500  # the user's own cap, sized down by risk later
    assert upper_bound_quantity(None, D("10000"), D("101"), 25) == 75
    assert upper_bound_quantity(None, D("0"), D("101"), None) == 0
    assert upper_bound_quantity(None, D("-5"), D("101"), None) == 0


# ---- position context from fills -----------------------------------------------------------------------------------


def test_position_context_follows_simulated_fill_times():
    bars = [ist(DAYS[0], 9, 15) + timedelta(minutes=5 * i) for i in range(6)]
    fills = [FillPoint(bars[2], OrderSide.BUY, 10), FillPoint(bars[4], OrderSide.SELL, 10)]
    ctx = contexts_for_bars(bars, fills)
    assert [ctx[b].is_open for b in bars] == [False, False, True, True, False, False]
    assert [ctx[b].closed_this_bar for b in bars] == [False, False, False, False, True, False]


def test_a_partial_exit_keeps_the_position_open():
    bars = [ist(DAYS[0], 9, 15) + timedelta(minutes=5 * i) for i in range(4)]
    ctx = contexts_for_bars(bars, [FillPoint(bars[0], OrderSide.BUY, 10), FillPoint(bars[2], OrderSide.SELL, 4)])
    assert [ctx[b].is_open for b in bars] == [True, True, True, True]


def test_fills_implying_a_short_position_are_refused():
    bars = [ist(DAYS[0], 9, 15)]
    with pytest.raises(ValueError):
        contexts_for_bars(bars, [FillPoint(bars[0], OrderSide.SELL, 1)])


# ---- lifecycle and configuration -----------------------------------------------------------------------------------


def test_the_transition_table_matches_the_database_trigger():
    S = AgentStatus
    assert ALLOWED == {
        S.STOPPED: {S.RUNNING},
        S.RUNNING: {S.PAUSED, S.STOPPED, S.ERROR},
        S.PAUSED: {S.RUNNING, S.STOPPED, S.ERROR},
        S.ERROR: {S.RUNNING, S.STOPPED},
    }


def test_configuration_fingerprints_ignore_order():
    c = [{"config_id": "b", "strategy_id": "VWAP", "params": {}, "priority": 2},
         {"config_id": "a", "strategy_id": "ORB", "params": {}, "priority": 1}]
    a = AgentConfigInput([2, 1], "REGIME_PRIORITY", c)
    b = AgentConfigInput([1, 2], "REGIME_PRIORITY", list(reversed(c)))
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != AgentConfigInput([1, 2], "REGIME_PRIORITY", c, warmup_sessions=3).fingerprint()


def test_a_new_version_takes_effect_from_the_next_session():
    assert next_session(CAL, ist(DAYS[0], 8, 0)) == DAYS[0]  # before today's open
    assert next_session(CAL, ist(DAYS[0], 10, 0)) == DAYS[1]
    assert next_session(CAL, ist(date(2026, 1, 9), 16, 0)) == date(2026, 1, 12)  # Friday -> Monday


# ---- shared warm-up rule (D13) --------------------------------------------------------------------------------------


def test_the_corporate_action_floor_truncates_the_warm_up():
    prior = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5)]
    floor = corporate_action_floor([(date(2026, 1, 2), "SPLIT"), (date(2026, 1, 3), "DIVIDEND")], date(2026, 1, 6))
    assert floor == date(2026, 1, 2)
    assert warmup_sessions_before(prior, floor, 3) == ([date(2026, 1, 2), date(2026, 1, 5)], date(2026, 1, 2))
    assert warmup_sessions_before(prior, None, 2) == ([date(2026, 1, 2), date(2026, 1, 5)], None)
    assert warmup_sessions_before(prior, None, 0) == ([], None)


# ---- replay: all five approved strategies ----------------------------------------------------------------------------


def archive(day: date, phase: float) -> list[ArchivedBar]:
    out = []
    for i in range(75):
        start = ist(day, 9, 15) + timedelta(minutes=5 * i)
        c = 100 + 3 * math.sin((i + phase) / 6) + 0.02 * i
        o = c - 0.3 * math.cos((i + phase) / 4)
        bar = IndicatorBar(start, day, o, max(o, c) + 0.4, min(o, c) - 0.4, c, 1000 + i)
        out.append(ArchivedBar(bar, 1000 * day.day + i, 1, "fixture", "UNVERIFIED_PROVIDER_NATIVE", D(repr(c))))
    return out


WARMUP = archive(DAYS[0], 0) + archive(DAYS[1], 11)
TODAY = archive(DAYS[2], 23)


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_replay_gives_exactly_the_strategys_own_signals(strategy_id):
    policy = build_policy("PINNED", [{"config_id": "c", "strategy_id": strategy_id, "params": {}}])
    outcomes, decision, candidates = replay(policy, CAL, 1, WARMUP, TODAY, {}, DAYS[2])
    assert decision is not None and decision.selected_config_id == "c" and set(candidates) == {"c"}

    direct = STRATEGIES[strategy_id].create(None, CAL)
    expected = [direct.evaluate(b.bar, FLAT) for b in WARMUP + TODAY][len(WARMUP):]
    acted = [(o.archived.start, o.signal) for o in outcomes if o.acting_config_id is not None]
    assert acted, "the selection must be decided within the session"
    assert any(sig.action is not Action.NONE for _start, sig in acted), "the data must exercise real signals"
    by_start = {s.bar_start: s for s in expected}
    for start, sig in acted:
        assert (sig.action, sig.reason, sig.reference_price) == (by_start[start].action, by_start[start].reason, by_start[start].reference_price)
    before = [o for o in outcomes if o.acting_config_id is None]
    assert all(o.archived.bar.start_ts + timedelta(minutes=5) <= ist(DAYS[2], 9, 30) for o in before)


def test_regime_priority_replay_matches_the_selector():
    candidates = [{"config_id": sid.lower(), "strategy_id": sid, "params": {}, "priority": i + 1}
                  for i, sid in enumerate(sorted(STRATEGIES))]
    policy = build_policy("REGIME_PRIORITY", candidates)
    _outcomes, decision, _ = replay(policy, CAL, 1, WARMUP, TODAY, {}, DAYS[2])
    selector = InstrumentSelector(1, policy, CAL)
    for b in WARMUP + TODAY:
        selector.observe(b.bar)
    expected = selector.decision(DAYS[2])
    assert decision.as_dict() == expected.as_dict()
