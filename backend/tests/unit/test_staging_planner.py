"""Staging scenario planning with realistic recording delays (pure: no database).

Real archived bars are always recorded after they close: the finalisation margin alone is 60 s, the live archive's
fastest observed lag was 143 s, and backfilled bars are recorded days later. These tests never use bars recorded at
the instant they close."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.exchange.pricing import session_date_of
from app.ops.staging import ArchivedBar, BarSeries, fill_slot, find_scenario, plan_leg

D = Decimal
STEP = 300
MAX_AGE = timedelta(seconds=900)
OPEN = datetime(2026, 11, 11, 3, 45, tzinfo=UTC)  # 09:15 IST
_ids = iter(range(1, 10_000))


def bar(i: int, lag_s: int = 143, *, session_start: datetime = OPEN, blocked: str | None = None, volume: int = 1000,
        inst: int = 1) -> ArchivedBar:
    start = session_start + timedelta(minutes=5 * i)
    end = start + timedelta(minutes=5)
    return ArchivedBar(
        id=next(_ids), instrument_id=inst, start=start, end=end, session_date=session_date_of(start),
        open=D(100 + i), close=D(100 + i) + D("0.5"), volume=volume, observed_at=end + timedelta(seconds=lag_s), blocked=blocked,
    )


def series(*bars: ArchivedBar) -> BarSeries:
    return BarSeries(bars)


def test_a_bar_is_available_only_once_recorded_not_at_its_close():
    b = bar(0, lag_s=143)
    assert b.available_at == b.end + timedelta(seconds=143)
    assert b.available_at > b.end


def test_a_bar_recorded_after_the_decision_is_invisible_however_old_it_is():
    b0, b1 = bar(0, lag_s=120), bar(1, lag_s=400)  # b1 ended at 09:25:00 but was recorded at 09:31:40
    s = series(b0, b1)
    at = b1.end + timedelta(seconds=60)  # b1 has closed, but is not recorded yet
    assert s.reference_at(at) == b0
    assert s.reference_at(b1.available_at) == b1


def test_no_bar_from_the_future_is_ever_a_reference():
    b0 = bar(0)
    s = series(b0)
    assert s.reference_at(b0.end - timedelta(seconds=1)) is None
    assert s.reference_at(b0.end) is None  # ended but not yet recorded
    assert s.reference_at(b0.available_at) == b0


def test_fill_slot_is_the_first_boundary_at_or_after_acceptance():
    assert fill_slot(datetime(2026, 11, 11, 3, 52, 23, tzinfo=UTC), STEP) == datetime(2026, 11, 11, 3, 55, tzinfo=UTC)
    assert fill_slot(datetime(2026, 11, 11, 3, 55, tzinfo=UTC), STEP) == datetime(2026, 11, 11, 3, 55, tzinfo=UTC)


def test_acceptance_after_the_next_bar_started_fills_on_the_bar_after_it():
    """The decision bar b0 is recorded 143 s after it closes; by then b1 has already started, so the order
    (accepted at b0.available_at) cannot fill on b1. The chronologically next bar is NOT the fill bar."""
    b0, b1, b2 = bar(0), bar(1), bar(2)
    leg, why = plan_leg(series(b0, b1, b2), b0.available_at, MAX_AGE, STEP)
    assert why == "ok"
    assert leg.reference == b0
    assert leg.fill == b2 and leg.fill != b1
    assert leg.fill.start >= leg.decided_at  # no fill on a bar that started before acceptance
    assert leg.processed_at == b2.available_at  # processing waits until the fill bar is recorded


def test_late_ingestion_makes_the_reference_stale_and_no_order_is_planned():
    b0, b1, b2, b3, b4 = bar(0, lag_s=960), bar(1, lag_s=960), bar(2, lag_s=960), bar(3, lag_s=960), bar(4, lag_s=960)
    leg, why = plan_leg(series(b0, b1, b2, b3, b4), b0.available_at, MAX_AGE, STEP)
    assert leg is None and why.startswith("STALE")


def test_a_reference_within_the_threshold_is_accepted_at_the_boundary():
    # b1 and b2 are not yet recorded when b0 is, so b0 (exactly 900 s old) is the newest visible reference.
    bars = [bar(0, lag_s=900), bar(1, lag_s=1200), bar(2, lag_s=1200)] + [bar(i) for i in range(3, 6)]
    b0 = bars[0]
    leg, why = plan_leg(series(*bars), b0.available_at, MAX_AGE, STEP)
    assert why == "ok" and leg.reference == b0
    assert leg.decided_at - b0.end == MAX_AGE
    # One second later the same reference is stale (and nothing newer is visible yet).
    late, why_late = plan_leg(series(*bars), b0.available_at + timedelta(seconds=1), MAX_AGE, STEP)
    assert late is None and why_late.startswith("STALE")


def test_a_missing_fill_bar_is_not_replaced_by_a_later_bar():
    b0, b1, b3 = bar(0), bar(1), bar(3)  # the 09:25 bar (slot of b2) is missing
    leg, why = plan_leg(series(b0, b1, b3), b0.available_at, MAX_AGE, STEP)
    assert leg is None and why.startswith("FILL_BAR_MISSING")


def test_a_fill_bar_in_the_next_session_is_not_a_fill():
    last = bar(74)  # 15:25-15:30 IST, the session's last bar
    next_day = bar(0, session_start=OPEN + timedelta(days=1))
    leg, why = plan_leg(series(last, next_day), last.available_at, MAX_AGE, STEP)
    assert leg is None and why.startswith("FILL_BAR_MISSING")


def test_ineligible_reference_or_fill_bars_are_not_planned():
    b0, b1, b2 = bar(0), bar(1), bar(2, blocked="recorded WARNING/ERROR data-quality issue")
    assert plan_leg(series(b0, b1, b2), b0.available_at, MAX_AGE, STEP)[1].startswith("FILL_BAR_INELIGIBLE")
    z = bar(2, volume=0)
    assert plan_leg(series(b0, b1, z), b0.available_at, MAX_AGE, STEP)[1] == "FILL_BAR_INELIGIBLE: zero volume"
    bad_ref = bar(0, blocked="corporate actions unavailable for its ingestion batch")
    assert plan_leg(series(bad_ref, bar(1), bar(2)), bad_ref.available_at, MAX_AGE, STEP)[1].startswith("REFERENCE_INELIGIBLE")


def test_a_purely_backfilled_archive_has_no_scenario():
    days_late = 3 * 86_400
    s = series(*[bar(i, lag_s=days_late) for i in range(30)])
    assert find_scenario({1: s}, MAX_AGE, STEP) is None


def test_scenario_uses_only_information_available_at_each_decision():
    lags = [143, 151, 160, 149, 171, 144, 158, 162, 150, 147, 155, 149]
    bars = [bar(i, lag_s=lag) for i, lag in enumerate(lags)]
    sc = find_scenario({1: series(*bars)}, MAX_AGE, STEP)
    assert sc is not None
    for leg in (sc.entry, sc.exit):
        assert leg.reference.available_at <= leg.decided_at  # the reference was recorded before the decision
        assert leg.decided_at - leg.reference.end <= MAX_AGE  # and was fresh
        assert leg.fill.start >= leg.decided_at  # the fill bar had not started at the decision
        assert leg.fill.start == fill_slot(leg.decided_at, STEP)
        assert leg.processed_at >= leg.fill.available_at  # nothing processed before the fill bar was recorded
    assert sc.exit.decided_at == sc.entry.processed_at
    assert sc.exit.fill.start > sc.entry.fill.start


def test_scenario_skips_a_round_trip_broken_by_a_gap_and_finds_an_earlier_one():
    bars = [bar(i) for i in range(10)]
    missing = bars.pop(8)  # gap near the end: the latest round trips cannot be completed
    sc = find_scenario({1: series(*bars)}, MAX_AGE, STEP)
    assert sc is not None
    assert missing.start not in (sc.entry.fill.start, sc.exit.fill.start)
    assert sc.entry.fill in bars and sc.exit.fill in bars


def test_only_recently_recorded_bars_are_used_in_a_mixed_archive():
    backfilled = [bar(i, lag_s=86_400 * 2) for i in range(40)]
    live = [bar(i, lag_s=150, session_start=OPEN + timedelta(days=3)) for i in range(8)]
    sc = find_scenario({1: series(*backfilled, *live)}, MAX_AGE, STEP)
    assert sc is not None
    assert sc.entry.reference in live and sc.entry.fill in live and sc.exit.fill in live


def test_the_old_instant_recording_assumption_would_have_picked_the_wrong_bar():
    """Documents the defect this module fixes: with zero-delay bars the next chronological bar fills; with a realistic
    delay it cannot, and a planner that assumed it would expect a fill that never happens."""
    instant = [bar(i, lag_s=0) for i in range(3)]
    realistic = [bar(i, lag_s=143) for i in range(3)]
    leg_instant, _ = plan_leg(series(*instant), instant[0].available_at, MAX_AGE, STEP)
    leg_real, _ = plan_leg(series(*realistic), realistic[0].available_at, MAX_AGE, STEP)
    assert leg_instant.fill.start == instant[1].start
    assert leg_real.fill.start == realistic[2].start


def test_longest_fresh_run_counts_only_contiguous_bars_recorded_within_the_limit():
    from app.ops.staging import freshness_report, longest_fresh_run

    bars = [bar(i, lag_s=150) for i in range(10)]
    bars[4] = bar(4, lag_s=901)  # recorded too late: breaks the run
    del bars[8]  # gap: breaks the run
    s = series(*bars)
    run = longest_fresh_run(s, MAX_AGE)
    assert [b.start for b in run] == [b.start for b in bars[:4]]
    rep = freshness_report({1: s}, MAX_AGE, 8)
    assert rep["best_run"] == 4 and rep["sufficient"] is False
    live = series(*[bar(i, lag_s=170) for i in range(8)])
    assert freshness_report({1: live}, MAX_AGE, 8)["sufficient"] is True
