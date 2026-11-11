"""The event-driven backtest engine (Phase 3, Step 4A): NEXT_BAR_OPEN_V1, long-only, one instrument at a time.

Each instrument is simulated independently with the full capital; instruments are never combined. Every bar
(warm-up and evaluation, in time order) goes through these phases exactly once:

  1. OPEN       A pending entry from the previous bar is entered at this bar's open. Then, with a position open:
                  a. protective stop gap-through: open <= stop -> exit at the open (STOP_GAP);
                  b. otherwise a pending strategy exit -> exit at the open (STRATEGY_EXIT / SESSION_SQUARE_OFF);
  2. INTRABAR   c. otherwise low <= stop -> exit at the stop (STOP); else high >= target -> exit at the target
                   (TARGET, also when the bar opened above it). The stop is checked first, so it wins when both are
                   touched. Checks start on the entry bar itself, after the entry.
  3. SELECTION  The bar goes to the session selector (decision locked at the 09:30 checkpoint, before any strategy
                sees a bar ending after it).
  4. STRATEGIES Every candidate evaluates the bar exactly once. The configuration holding the position gets the
                real position state; every other candidate gets FLAT and only keeps its indicators warm.
  5. ACT        ENTER from a configuration with `entry_permitted` (the session's locked selection), flat and nothing
                pending -> pending entry. EXIT from the position's owner -> pending exit. Everything else is ignored.
  6. SESSION    Using only the NEXT bar's timestamp (never its prices):
                  * position open and this is the session's last available bar -> exit at this bar's close
                    (SQUARE_OFF_FALLBACK_LAST_BAR, decision D5): never overnight;
                  * pending square-off and the intended square-off bar (the next slot) is missing -> exit at this
                    bar's close (SQUARE_OFF_FALLBACK_LAST_BAR, D5);
                  * pending entry and the next expected bar is missing / the session ends -> it expires;
                  * pending strategy exit and the next slot is missing -> deferred to the next available bar's open.
  7. MARK       Evaluation bars record cash + quantity x close, next to the buy-and-hold benchmark's value.

No trading in warm-up sessions. All money is Decimal; strategies see float64 copies of the same prices.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from app.backtest.benchmark import BenchmarkRun, assess_benchmark, run_benchmark
from app.backtest.costs import CostModel, Side, level_to_decimal, whole_shares
from app.backtest.data import PreparedInstrument, SlotIndex
from app.backtest.model import (
    BacktestBar,
    BacktestRequest,
    EquityPoint,
    Event,
    EventType,
    ExitReason,
    ExitTiming,
    InstrumentResult,
    InstrumentSummary,
    Trade,
)
from app.indicators.core import IndicatorBar
from app.marketdata.calendar import TradingCalendar
from app.strategies.base import Action, PositionContext, Reason, Signal, Strategy
from app.strategies.registry import STRATEGIES
from app.strategies.selection import Candidate, InstrumentSelector, SelectionStatus

StrategyFactory = Callable[[Candidate, TradingCalendar], Strategy]
PCT = Decimal("0.0001")


def default_strategy_factory(candidate: Candidate, calendar: TradingCalendar) -> Strategy:
    return STRATEGIES[candidate.strategy_id].create(candidate.config, calendar)


def indicator_bar(bar: BacktestBar) -> IndicatorBar:
    return IndicatorBar(bar.start_ts, bar.session_date, float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume))


def explanation(value):
    """A selection decision as JSON-ready values, like `SelectionDecision.as_dict()` but with every timestamp in UTC
    (`...Z`): the explanation of a decision must not depend on the time zone its inputs were expressed in."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, tuple):
        return [explanation(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: explanation(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator * 100 / denominator).quantize(PCT, rounding=ROUND_HALF_EVEN)


@dataclass
class _Pending:
    entry: bool  # True: entry, False: exit
    config_id: str
    signal: Signal
    signal_bar: BacktestBar


@dataclass
class _Position:
    config_id: str
    strategy_id: str
    strategy_version: str
    entry_signal: Signal
    entry_signal_bar: BacktestBar
    entry_bar: BacktestBar
    entry_price: Decimal
    quantity: int
    stop: Decimal | None
    target: Decimal | None
    entry_fees: Decimal


class InstrumentEngine:
    def __init__(
        self, request: BacktestRequest, prepared: PreparedInstrument, calendar: TradingCalendar,
        strategy_factory: StrategyFactory = default_strategy_factory,
    ) -> None:
        self.request = request
        self.prepared = prepared
        self.calendar = calendar
        self.costs: CostModel = request.cost_model
        self.selector = InstrumentSelector(prepared.instrument_id, request.policy, calendar)
        self.candidates = request.candidates
        self.strategies = {c.config_id: strategy_factory(c, calendar) for c in self.candidates}
        self.slots = SlotIndex(calendar)
        self.cash = request.capital
        self.position: _Position | None = None
        self.pending: _Pending | None = None
        self.trades: list[Trade] = []
        self.events: list[Event] = []
        self.equity: list[EquityPoint] = []
        self.decisions: list = []
        self.ignored_entries = 0

    # ---- records ------------------------------------------------------------------------------------------------

    def _event(self, bar: BacktestBar, kind: EventType, config_id: str | None = None, **detail) -> None:
        self.events.append(Event(len(self.events) + 1, bar.start_ts, bar.session_date, kind, config_id, detail))

    def _enter(self, bar: BacktestBar, pending: _Pending) -> None:
        price = self.costs.price(Side.BUY, bar.open)
        quantity = whole_shares(self.cash, price, self.costs)
        if quantity == 0:
            self._event(bar, EventType.ENTRY_SKIPPED_INSUFFICIENT_CAPITAL, pending.config_id, cash=self.cash, price=price)
            return
        notional = quantity * price
        fees = self.costs.fees(Side.BUY, notional)
        self.cash -= notional + fees
        levels = pending.signal.protective
        stop = target = None
        if levels is not None:
            if levels.stop_price is not None:
                stop = level_to_decimal(levels.stop_price)
            elif levels.stop_distance is not None:
                stop = price - level_to_decimal(levels.stop_distance)  # anchored to the simulated entry price
            if levels.target_distance is not None:
                target = price + level_to_decimal(levels.target_distance)
        self.position = _Position(
            pending.config_id, pending.signal.strategy_id, pending.signal.strategy_version, pending.signal, pending.signal_bar,
            bar, price, quantity, stop, target, fees,
        )

    def _exit(self, bar: BacktestBar, raw: Decimal, reason: ExitReason, timing: ExitTiming, pending: _Pending | None = None) -> str:
        p = self.position
        price = self.costs.price(Side.SELL, raw)
        proceeds = p.quantity * price
        fees = self.costs.fees(Side.SELL, proceeds)
        self.cash += proceeds - fees
        gross = p.quantity * (price - p.entry_price)
        self.trades.append(
            Trade(
                seq=len(self.trades) + 1,
                instrument_id=self.prepared.instrument_id,
                session_date=bar.session_date,
                config_id=p.config_id,
                strategy_id=p.strategy_id,
                strategy_version=p.strategy_version,
                entry_signal_bar_start=p.entry_signal_bar.start_ts,
                entry_signal_reason=p.entry_signal.reason.value,
                entry_reference_price=p.entry_signal_bar.close,
                entry_bar_start=p.entry_bar.start_ts,
                entry_price=p.entry_price,
                quantity=p.quantity,
                stop_level=p.stop,
                target_level=p.target,
                entry_fees=p.entry_fees,
                exit_signal_bar_start=None if pending is None else pending.signal_bar.start_ts,
                exit_signal_reason=None if pending is None else pending.signal.reason.value,
                exit_reference_price=None if pending is None else pending.signal_bar.close,
                exit_bar_start=bar.start_ts,
                exit_timing=timing,
                exit_price=price,
                exit_reason=reason,
                exit_fees=fees,
                gross_pnl=gross,
                net_pnl=gross - p.entry_fees - fees,
            )
        )
        self.position = None
        return p.config_id

    # ---- the bar loop -------------------------------------------------------------------------------------------

    def _open_and_intrabar(self, bar: BacktestBar) -> str | None:
        """Phases 1-2. Returns the config_id whose position closed during this bar, if any."""
        pending, self.pending = self.pending, None
        if pending is not None and pending.entry:
            self._enter(bar, pending)
            pending = None
        p = self.position
        if p is None:
            return None
        if p.stop is not None and bar.open <= p.stop:
            return self._exit(bar, bar.open, ExitReason.STOP_GAP, ExitTiming.AT_OPEN)
        if pending is not None:
            reason = ExitReason.SESSION_SQUARE_OFF if pending.signal.reason is Reason.SESSION_SQUARE_OFF else ExitReason.STRATEGY_EXIT
            return self._exit(bar, bar.open, reason, ExitTiming.AT_OPEN, pending)
        if p.stop is not None and bar.low <= p.stop:
            return self._exit(bar, p.stop, ExitReason.STOP, ExitTiming.INTRABAR)
        if p.target is not None and bar.high >= p.target:
            return self._exit(bar, p.target, ExitReason.TARGET, ExitTiming.INTRABAR)
        return None

    def _act(self, bar: BacktestBar, evaluating: bool, signals: dict[str, Signal]) -> None:
        for candidate in self.candidates:
            cid, signal = candidate.config_id, signals[candidate.config_id]
            if signal.action is Action.ENTER_LONG:
                if evaluating and self.position is None and self.pending is None and self.selector.entry_permitted(cid, bar.session_date):
                    self.pending = _Pending(True, cid, signal, bar)
                    self._event(bar, EventType.ENTRY_SIGNAL, cid, reason=signal.reason.value, reference_price=bar.close, protective=signal.protective)
                elif evaluating:
                    self.ignored_entries += 1
            elif signal.action is Action.EXIT_LONG:
                if self.position is None or self.position.config_id != cid:  # only the owner is ever told it is open
                    raise AssertionError(f"exit signal from {cid}, which holds no position")
                self.pending = _Pending(False, cid, signal, bar)
                self._event(bar, EventType.EXIT_SIGNAL, cid, reason=signal.reason.value, reference_price=bar.close)

    def _session_rules(self, bar: BacktestBar, nxt: BacktestBar | None) -> str | None:
        """Phase 6. Returns the config_id whose position was closed at this bar's close, if any."""
        session_ends = nxt is None or nxt.session_date != bar.session_date
        contiguous = not session_ends and self.slots.of(nxt) == self.slots.of(bar) + 1
        pending = self.pending
        if self.position is not None:
            square_off_due = pending is not None and not pending.entry and pending.signal.reason is Reason.SESSION_SQUARE_OFF
            if session_ends or (square_off_due and not contiguous):
                self.pending = None
                return self._exit(bar, bar.close, ExitReason.SQUARE_OFF_FALLBACK_LAST_BAR, ExitTiming.AT_CLOSE, pending)
            if pending is not None and not contiguous:
                self._event(bar, EventType.EXIT_DEFERRED_MISSING_BAR, pending.config_id, next_bar_start=nxt.start_ts)
        elif pending is not None and pending.entry and not contiguous:
            kind = EventType.ENTRY_EXPIRED_SESSION_END if session_ends else EventType.ENTRY_EXPIRED_MISSING_BAR
            self._event(bar, kind, pending.config_id, signal_bar_start=bar.start_ts)
            self.pending = None
        return None

    def run(self) -> InstrumentResult:
        bars = self.prepared.bars
        evaluation = self.prepared.evaluation_sessions
        bench: BenchmarkRun = run_benchmark(self.prepared.evaluation_bars, self.request.capital, self.costs)
        bench_marks = iter(bench.marks)
        closed_at_previous_close: str | None = None
        previous: BacktestBar | None = None
        held_bars = 0
        for i, bar in enumerate(bars):
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            evaluating = bar.session_date in evaluation
            new_session = previous is None or previous.session_date != bar.session_date
            if new_session:
                closed_at_previous_close = None  # a session-end close never starts a cooldown in the next session
            if evaluating:
                expected_slot = 0 if new_session else self.slots.of(previous) + 1
                gap = self.slots.of(bar) - expected_slot
                if gap > 0:
                    first_missing = sorted(self.slots.slots(bar.session_date))[expected_slot]
                    self._event(bar, EventType.MISSING_BARS, None, count=gap, first_missing_start=first_missing)

            closed = self._open_and_intrabar(bar) or closed_at_previous_close
            ibar = indicator_bar(bar)
            decision = self.selector.observe(ibar)
            if decision is not None and evaluating:
                self.decisions.append(explanation(decision))
            signals = {}
            for candidate in self.candidates:
                cid = candidate.config_id
                is_open = self.position is not None and self.position.config_id == cid
                signals[cid] = self.strategies[cid].evaluate(ibar, PositionContext(is_open=is_open, closed_this_bar=closed == cid and not is_open))
            self._act(bar, evaluating, signals)
            closed_at_previous_close = self._session_rules(bar, nxt)

            if evaluating:
                if nxt is None or nxt.session_date != bar.session_date:
                    if self.selector.decision(bar.session_date) is None:
                        self._event(bar, EventType.NO_SELECTION_DECISION, None, note="session data ended before the selection checkpoint")
                quantity = 0 if self.position is None else self.position.quantity
                held_bars += quantity > 0
                self.equity.append(
                    EquityPoint(bar.start_ts, bar.session_date, self.cash, quantity, bar.close, self.cash + quantity * bar.close, next(bench_marks))
                )
            previous = bar
        if self.position is not None or self.pending is not None:
            raise AssertionError("a position or order survived the end of the data")  # phase 6 always resolves both
        return self._result(bench, held_bars)

    # ---- summary ------------------------------------------------------------------------------------------------

    def _result(self, bench: BenchmarkRun, held_bars: int) -> InstrumentResult:
        capital = self.request.capital
        final = self.equity[-1].equity
        peak, max_dd = capital, Decimal(0)
        for point in self.equity:
            peak = max(peak, point.equity)
            max_dd = max(max_dd, (peak - point.equity) / peak)
        sessions = sorted(self.prepared.evaluation_sessions)
        selected = sum(1 for d in sessions if (dec := self.selector.decision(d)) is not None and dec.status is SelectionStatus.SELECTED)
        benchmark = assess_benchmark(bench, self.prepared.quality, capital)
        summary = InstrumentSummary(
            starting_capital=capital,
            final_equity=final,
            return_pct=pct(final - capital, capital),
            trades=len(self.trades),
            winning_trades=sum(1 for t in self.trades if t.net_pnl > 0),
            losing_trades=sum(1 for t in self.trades if t.net_pnl < 0),
            total_fees=sum((t.entry_fees + t.exit_fees for t in self.trades), Decimal(0)),
            max_drawdown_pct=(max_dd * 100).quantize(PCT, rounding=ROUND_HALF_EVEN),
            exposure_pct=pct(Decimal(held_bars), Decimal(len(self.equity))),
            sessions_with_selection=selected,
            sessions_without_selection=len(sessions) - selected,
            entry_signals_ignored=self.ignored_entries,
            benchmark_return_pct=benchmark.return_pct,
        )
        return InstrumentResult(
            self.prepared.instrument_id, self.prepared.quality, tuple(self.decisions), tuple(self.trades), tuple(self.events),
            tuple(self.equity), benchmark, summary,
        )
