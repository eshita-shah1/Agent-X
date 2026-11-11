"""The independent buy-and-hold benchmark: numbers by hand, costs, validity, and its separation from selection."""

from app.backtest import BenchmarkStatus, CorporateActionRecord, CostModel
from tests.backtest import D, NEXT_DAY, THIRD_DAY, local, run, session


def test_buys_at_the_first_evaluation_open_and_marks_every_bar():
    bars = session(NEXT_DAY, overrides={"09:15": (100, 101, 100, 101), "15:25": (110,)})
    r = run(bars)
    b = r.benchmark
    assert (local(b.entry_bar_start), b.entry_price, b.quantity, b.entry_fees) == ("09:15", D(100), 1000, D(0))
    assert r.equity[0].benchmark_equity == D(101000) and r.equity[-1].benchmark_equity == D(110000)
    assert (b.last_close, b.hypothetical_exit_price, b.final_value, b.return_pct) == (D(110), D(110), D(110000), D("10.0000"))
    assert r.summary.benchmark_return_pct == D("10.0000")


def test_entry_and_hypothetical_exit_costs_are_applied():
    costs = CostModel(fee_per_order=D(10), slippage_bps=D(10))
    r = run(session(NEXT_DAY, overrides={"15:25": (110,)}), costs=costs)
    b = r.benchmark
    assert (b.entry_price, b.quantity, b.entry_fees) == (D("100.1"), 998, D(10))  # 999 x 100.1 + 10 > 100000
    cash = D(100000) - 998 * D("100.1") - 10
    assert (b.hypothetical_exit_price, b.hypothetical_exit_fees) == (D("109.89"), D(10))
    assert b.final_value == cash + 998 * D("109.89") - 10
    assert r.equity[-1].benchmark_equity == cash + 998 * D(110)  # marks exclude the hypothetical exit


def test_a_complete_single_session_is_valid():
    b = run(session(NEXT_DAY)).benchmark
    assert (b.status, b.limitations) == (BenchmarkStatus.VALID, ())
    assert b.notes == ("UNVERIFIED_PRICE_BASIS", "PER_INSTRUMENT_NOT_PORTFOLIO")


def test_limitations_make_it_limited():
    bars = session(NEXT_DAY, skip={"10:00"}) + session(THIRD_DAY)
    b = run(bars, end=THIRD_DAY, actions=[CorporateActionRecord(THIRD_DAY, "DIVIDEND")]).benchmark
    assert b.status is BenchmarkStatus.LIMITED
    assert b.limitations == ("DIVIDENDS_EXCLUDED", "MISSING_BARS")
    assert "HOLDS_OVERNIGHT_UNLIKE_THE_INTRADAY_STRATEGY" in b.notes
    tiny = run(session(NEXT_DAY), capital="50").benchmark
    assert tiny.quantity == 0 and "INSUFFICIENT_CAPITAL_FOR_ONE_SHARE" in tiny.limitations


def test_the_benchmark_does_not_depend_on_the_strategy():
    bars = session(NEXT_DAY, overrides={"10:30": (100, 100.5, 98.5, 99.5)})
    traded, idle = run(bars, {"10:00": None}), run(bars)
    assert traded.benchmark == idle.benchmark
    assert [p.benchmark_equity for p in traded.equity] == [p.benchmark_equity for p in idle.equity]
