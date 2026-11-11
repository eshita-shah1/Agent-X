"""Request validation, data validation, warm-up selection, corporate-action handling and data-quality disclosure."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.backtest import (
    BacktestBar,
    BacktestRefused,
    BacktestRequest,
    CorporateActionRecord,
    CostModel,
    InstrumentData,
    RefusalCode,
    WarmupSource,
    prepare_backtest,
)
from app.strategies import EMACrossoverConfig, ORBConfig
from app.strategies.selection import Candidate, PinnedPolicy, RegimePriorityPolicy
from tests.backtest import D, DAY, NEXT_DAY, POLICY, THIRD_DAY, bar, local, run, session
from tests.indicators import ist

D0, D1, D2, D3, D4 = date(2026, 10, 29), date(2026, 10, 30), DAY, NEXT_DAY, THIRD_DAY  # Thu, Fri, Mon, Tue, Wed
FIFTH = date(2026, 11, 5)


def request(start=D3, end=FIFTH, warmup=2, **kw):
    return BacktestRequest(("X",), start, end, kw.pop("policy", POLICY), kw.pop("capital", D(100000)), warmup_sessions=warmup, **kw)


def prepare(bars, req=None, actions=()):
    (p,) = prepare_backtest(req or request(), {"X": InstrumentData("X", tuple(bars), tuple(actions))})
    return p


def days(*ds, **kw):
    return [b for d in ds for b in session(d, **kw)]


# ---- warm-up ------------------------------------------------------------------------------------------------------


def test_two_prior_sessions_are_the_warmup_and_older_bars_are_ignored():
    p = prepare(days(D0, D1, D2, D3, D4, FIFTH))
    q = p.quality
    assert (q.warmup_source, q.warmup_session_dates, q.warmup_bars) == (WarmupSource.PRIOR_SESSIONS, (D1, D2), 150)
    assert (q.effective_start, q.effective_end, q.evaluation_sessions, q.bars_ignored_outside_window) == (D3, FIFTH, 3, 75)


def test_without_prior_history_the_warmup_shifts_into_the_period_and_is_disclosed():
    q = prepare(days(D3, D4, FIFTH)).quality
    assert (q.warmup_source, q.warmup_session_dates, q.effective_start, q.evaluation_sessions) == (WarmupSource.SHIFTED_INTO_PERIOD, (D3, D4), FIFTH, 1)
    q = prepare(days(D2, D3, D4, FIFTH)).quality
    assert (q.warmup_source, q.warmup_session_dates, q.effective_start) == (WarmupSource.PRIOR_AND_SHIFTED, (D2, D3), D4)


def test_zero_warmup_and_insufficient_data():
    assert prepare(days(D3), request(warmup=0)).quality.warmup_source is WarmupSource.NONE
    with pytest.raises(BacktestRefused) as exc:
        prepare(days(D3, D4), request(warmup=2))
    assert exc.value.code is RefusalCode.INSUFFICIENT_DATA
    with pytest.raises(BacktestRefused) as exc:
        prepare(days(D1, D2), request())
    assert exc.value.code is RefusalCode.NO_DATA


def test_no_trading_happens_in_warmup_sessions():
    r = run(days(D2, D3), {(D2, "10:00"): None, (D3, "10:00"): None}, warmup=1, start=D3, end=D3)
    assert [t.session_date for t in r.trades] == [D3]
    assert {p.session_date for p in r.equity} == {D3} and [d["session_date"] for d in r.selection_decisions] == ["2026-11-03"]


# ---- corporate actions (D11 and the warm-up truncation) -----------------------------------------------------------


@pytest.mark.parametrize("kind", ["SPLIT", "BONUS"])
@pytest.mark.parametrize("ex_date", [D3, D4, FIFTH])
def test_a_split_or_bonus_inside_the_period_refuses(kind, ex_date):
    with pytest.raises(BacktestRefused) as exc:
        prepare(days(D1, D2, D3, D4, FIFTH), actions=[CorporateActionRecord(ex_date, kind)])
    assert exc.value.code is RefusalCode.CORPORATE_ACTION_IN_PERIOD and exc.value.instrument_id == "X"


def test_a_split_in_the_warmup_drops_the_older_history_and_shifts_the_start():
    q = prepare(days(D1, D2, D3, D4, FIFTH), actions=[CorporateActionRecord(D2, "SPLIT")]).quality
    assert q.warmup_truncated_by_corporate_action == D2
    assert (q.warmup_source, q.warmup_session_dates, q.effective_start) == (WarmupSource.PRIOR_AND_SHIFTED, (D2, D3), D4)


def test_a_split_before_the_warmup_window_changes_nothing():
    q = prepare(days(D1, D2, D3, D4, FIFTH), actions=[CorporateActionRecord(D0, "SPLIT")]).quality
    assert q.warmup_truncated_by_corporate_action is None and q.warmup_session_dates == (D1, D2)


def test_dividends_and_other_actions_are_disclosed_not_refused():
    actions = [CorporateActionRecord(D4, "DIVIDEND", "5 INR"), CorporateActionRecord(FIFTH, "OTHER"), CorporateActionRecord(D0, "DIVIDEND")]
    q = prepare(days(D1, D2, D3, D4, FIFTH), actions=actions).quality
    assert [(a.ex_date, a.action_type) for a in q.corporate_actions_disclosed] == [(D4, "DIVIDEND"), (FIFTH, "OTHER")]


# ---- data validation ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        lambda: bar(D3, "09:17", 100),  # off the 5-minute grid
        lambda: bar(D3, "15:30", 100),  # after the last slot
        lambda: bar(date(2026, 11, 7), "10:00", 100),  # a Saturday
    ],
)
def test_bars_off_the_calendar_grid_are_refused(bad):
    with pytest.raises(BacktestRefused) as exc:
        prepare(days(D1, D2, D3) + [bad()])
    assert exc.value.code is RefusalCode.INVALID_DATA


def test_out_of_order_and_duplicate_bars_are_refused():
    bars = days(D1, D2, D3)
    for broken in (bars[:100] + [bars[99]] + bars[100:], bars[:100] + [bars[101], bars[100]] + bars[102:]):
        with pytest.raises(BacktestRefused) as exc:
            prepare(broken)
        assert exc.value.code is RefusalCode.INVALID_DATA


def test_bar_values_are_validated():
    ts = ist(D3, 10, 0)
    with pytest.raises(ValueError, match="Decimal"):
        BacktestBar(ts, D3, 100.0, D(101), D(99), D(100), 1)
    with pytest.raises(ValueError, match="inconsistent"):
        BacktestBar(ts, D3, D(100), D(99), D(98), D(100), 1)
    with pytest.raises(ValueError, match="> 0"):
        BacktestBar(ts, D3, D(0), D(1), D(0), D(1), 1)
    with pytest.raises(ValueError, match="session_date"):
        BacktestBar(ts, D4, D(100), D(100), D(100), D(100), 1)
    with pytest.raises(ValueError, match="timezone"):
        BacktestBar(datetime(2026, 11, 3, 10), D3, D(100), D(100), D(100), D(100), 1)
    with pytest.raises(ValueError, match="volume"):
        BacktestBar(ts, D3, D(100), D(100), D(100), D(100), -1)


def test_missing_instrument_data_is_refused():
    req = BacktestRequest(("X", "Y"), D3, D3, POLICY, D(1000), warmup_sessions=0)
    with pytest.raises(BacktestRefused) as exc:
        prepare_backtest(req, {"X": InstrumentData("X", tuple(days(D3)))})
    assert (exc.value.code, exc.value.instrument_id) == (RefusalCode.NO_DATA, "Y")


# ---- request validation -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"start": D4, "end": D3}, RefusalCode.INVALID_PERIOD),
        ({"start": ist(D3, 9, 15)}, RefusalCode.INVALID_PERIOD),
        ({"capital": D(0)}, RefusalCode.INVALID_CONFIGURATION),
        ({"capital": 1000.0}, RefusalCode.INVALID_CONFIGURATION),
        ({"warmup": -1}, RefusalCode.INVALID_CONFIGURATION),
        ({"warmup": 21}, RefusalCode.INVALID_CONFIGURATION),
        ({"timeframe": "1m"}, RefusalCode.INVALID_CONFIGURATION),
        ({"cost_model": "zero"}, RefusalCode.INVALID_CONFIGURATION),
        ({"policy": PinnedPolicy(Candidate("bad", "ORB", EMACrossoverConfig()))}, RefusalCode.INVALID_CONFIGURATION),
        ({"policy": RegimePriorityPolicy((Candidate("a", "ORB", ORBConfig(), 1), Candidate("b", "MACD", ORBConfig(), 2)))}, RefusalCode.INVALID_CONFIGURATION),
    ],
)
def test_invalid_requests_are_refused(kwargs, code):
    with pytest.raises(BacktestRefused) as exc:
        request(**kwargs)
    assert exc.value.code is code


def test_request_values_are_normalised_and_frozen():
    req = request(capital=100000)
    assert type(req.capital) is Decimal and req.capital == 100000
    with pytest.raises(Exception):
        req.capital = D(1)  # type: ignore[misc]
    with pytest.raises(ValueError):
        CostModel(slippage_bps=-1)
    with pytest.raises(ValueError):
        CostModel(fee_per_order=0.5)  # floats never enter money


# ---- data-quality disclosure --------------------------------------------------------------------------------------


def test_coverage_missing_bars_and_sessions_without_data():
    bars = session(D3, skip={"10:00", "10:05"}) + session(FIFTH)  # D4 has no data at all
    q = prepare(bars, request(warmup=0)).quality
    assert (q.expected_bars, q.evaluation_bars, q.missing_bars, q.sessions_without_data) == (225, 148, 77, (D4,))
    assert q.coverage_pct == D("65.78")
    assert [local(t) for t in q.missing_bar_starts[:2]] == ["10:00", "10:05"]
