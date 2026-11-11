"""Independent buy-and-hold benchmark for one instrument.

Same capital, cost model and whole-share sizing as the strategy. Buys at the open of the first evaluation bar (never
during warm-up), holds through every session (overnight included, unlike the strategy) and is marked at every
evaluation bar's close. Its final value deducts a hypothetical exit at the last close, with slippage and fees
(approved decision 4). It is a comparison only: nothing here feeds selection or configuration (decision D15).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from app.backtest.costs import CostModel, Side, whole_shares
from app.backtest.model import BacktestBar, BenchmarkResult, BenchmarkStatus, DataQuality


@dataclass(frozen=True)
class BenchmarkRun:
    entry_bar_start: datetime
    entry_price: Decimal
    quantity: int
    entry_fees: Decimal
    cash: Decimal
    marks: tuple[Decimal, ...]  # cash + quantity x close, one per evaluation bar
    last_close: Decimal
    exit_price: Decimal
    exit_fees: Decimal
    final_value: Decimal


def run_benchmark(bars: tuple[BacktestBar, ...], capital: Decimal, costs: CostModel) -> BenchmarkRun:
    first, last = bars[0], bars[-1]
    price = costs.price(Side.BUY, first.open)
    quantity = whole_shares(capital, price, costs)
    notional = quantity * price
    entry_fees = costs.fees(Side.BUY, notional)
    cash = capital - notional - entry_fees
    marks = tuple(cash + quantity * b.close for b in bars)
    exit_price = costs.price(Side.SELL, last.close)
    proceeds = quantity * exit_price
    exit_fees = costs.fees(Side.SELL, proceeds)
    return BenchmarkRun(first.start_ts, price, quantity, entry_fees, cash, marks, last.close, exit_price, exit_fees, cash + proceeds - exit_fees)


def assess_benchmark(run: BenchmarkRun, quality: DataQuality, capital: Decimal) -> BenchmarkResult:
    limitations = []
    if run.quantity == 0:
        limitations.append("INSUFFICIENT_CAPITAL_FOR_ONE_SHARE")
    if any(a.action_type == "DIVIDEND" for a in quality.corporate_actions_disclosed):
        limitations.append("DIVIDENDS_EXCLUDED")
    if any(a.action_type == "OTHER" for a in quality.corporate_actions_disclosed):
        limitations.append("OTHER_CORPORATE_ACTION_NOT_ADJUSTED")
    if quality.missing_bars:
        limitations.append("MISSING_BARS")
    if quality.sessions_without_data:
        limitations.append("SESSIONS_WITHOUT_DATA")
    notes = ["UNVERIFIED_PRICE_BASIS", "PER_INSTRUMENT_NOT_PORTFOLIO"]
    if quality.evaluation_sessions > 1:
        notes.append("HOLDS_OVERNIGHT_UNLIKE_THE_INTRADAY_STRATEGY")
    return BenchmarkResult(
        status=BenchmarkStatus.LIMITED if limitations else BenchmarkStatus.VALID,
        limitations=tuple(limitations),
        notes=tuple(notes),
        entry_bar_start=run.entry_bar_start,
        entry_price=run.entry_price,
        quantity=run.quantity,
        entry_fees=run.entry_fees,
        last_close=run.last_close,
        hypothetical_exit_price=run.exit_price,
        hypothetical_exit_fees=run.exit_fees,
        final_value=run.final_value,
        return_pct=((run.final_value - capital) * 100 / capital).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN),
    )
