"""Determinism, no lookahead and truncation invariance of selection over a multi-session series."""

import dataclasses
import math

from app.strategies.selection import InstrumentSelector, PinnedPolicy, RegimePriorityPolicy
from tests.indicators import ist
from tests.strategies import DAY, NEXT_DAY, THIRD_DAY, pbar
from tests.strategies.selection import ALL, EMA

MISSING = {(1, 2), (2, 0)}  # day 2 misses its 09:25 bar; day 3 misses its 09:15 bar


def series():
    out = []
    for s, day in enumerate((DAY, NEXT_DAY, THIRD_DAY)):
        for slot in range(75):
            if (s, slot) in MISSING:
                continue
            i = s * 75 + slot
            out.append(pbar(day, slot, 100 + 6 * math.sin(i / 25) + 0.8 * math.sin(i / 3), volume=500 + i % 300))
    return out


BARS = series()
POLICIES = {"regime": RegimePriorityPolicy(ALL), "pinned": PinnedPolicy(EMA)}


def run_selector(policy, bars):
    sel = InstrumentSelector("X", policy)
    emitted = [sel.observe(b) for b in bars]
    return [None if d is None else d.as_dict() for d in emitted]


def test_decisions_are_identical_on_repeat():
    for policy in POLICIES.values():
        assert run_selector(policy, BARS) == run_selector(policy, BARS)


def test_truncation_invariance_of_the_decision_stream():
    for policy in POLICIES.values():
        full = run_selector(policy, BARS)
        for t in range(len(BARS)):
            assert run_selector(policy, BARS[: t + 1]) == full[: t + 1], t


def test_bars_after_a_checkpoint_cannot_change_that_sessions_decision():
    for policy in POLICIES.values():
        base = InstrumentSelector("X", policy)
        for b in BARS:
            base.observe(b)
        cut = next(i for i, b in enumerate(BARS) if b.session_date == NEXT_DAY and b.start_ts >= ist(NEXT_DAY, 9, 30))  # ends after 09:30
        perturbed = BARS[:cut] + [dataclasses.replace(b, high=b.high * 3, close=b.close * 2.5, open=b.open * 2) for b in BARS[cut:]]
        other = InstrumentSelector("X", policy)
        for b in perturbed:
            other.observe(b)
        assert other.decision(NEXT_DAY).as_dict() == base.decision(NEXT_DAY).as_dict()


def test_every_session_reaching_its_checkpoint_gets_exactly_one_decision():
    for policy in POLICIES.values():
        emitted = [d for d in run_selector(policy, BARS) if d is not None]
        assert [d["session_date"] for d in emitted] == ["2026-11-02", "2026-11-03", "2026-11-04"]
        assert emitted[1]["decided_on_bar_start"] == "2026-11-03T09:30:00+05:30"  # 09:25 missing: next bar triggers
        assert emitted[1]["regime"]["input_bar_start"] == "2026-11-03T09:20:00+05:30"  # ...without being used
