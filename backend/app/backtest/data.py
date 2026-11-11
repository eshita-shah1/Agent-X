"""Historical data preparation and data-quality validation for one instrument.

Refusals (BacktestRefused, before any simulation):
  * a bar that is not a BacktestBar, not on its session's 5-minute calendar grid, or out of strict time order;
  * a SPLIT or BONUS ex-date inside the requested evaluation period (decision D11);
  * no bars in the requested period (NO_DATA), or none left after the warm-up (INSUFFICIENT_DATA).

Warm-up (approved decision 3): the last `warmup_sessions` sessions with data before `start` feed the indicators and
the regime classifier without trading. If fewer exist, the shortfall is taken from the first sessions of the
requested period and the effective start moves forward (disclosed as SHIFTED_INTO_PERIOD).

Split/bonus before the period (additional handling, NOT a refusal; D11 is unchanged): warm-up bars dated before the
latest split/bonus ex-date at or before `start` are dropped, because unadjusted prices from both sides of the event
would corrupt continuous indicators (ATR, EMA, ADX, Supertrend) that carry into the evaluation period. The ex-date
is disclosed as `warmup_truncated_by_corporate_action`, and any shortfall is shifted into the period as above.

Nothing is filled, interpolated or adjusted. Dividends and OTHER actions in the period are disclosed only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from app.backtest.model import (
    REFUSED_ACTION_TYPES,
    TIMEFRAME,
    BacktestBar,
    BacktestRefused,
    BacktestRequest,
    DataQuality,
    InstrumentData,
    RefusalCode,
    WarmupSource,
)
from app.marketdata.calendar import TradingCalendar

MAX_LISTED_MISSING = 500  # missing bar timestamps listed individually; the count is always complete


@dataclass(frozen=True)
class PreparedInstrument:
    instrument_id: object
    warmup_bars: tuple[BacktestBar, ...]
    evaluation_bars: tuple[BacktestBar, ...]
    corporate_actions: tuple
    quality: DataQuality

    @property
    def bars(self) -> tuple[BacktestBar, ...]:
        return self.warmup_bars + self.evaluation_bars

    @property
    def evaluation_sessions(self) -> frozenset[date]:
        return frozenset(b.session_date for b in self.evaluation_bars)


class SlotIndex:
    """Each bar's position among its session's expected 5-minute slots (cached per session)."""

    def __init__(self, calendar: TradingCalendar) -> None:
        self.calendar = calendar
        self._cache: dict[date, dict[datetime, int]] = {}

    def slots(self, day: date) -> dict[datetime, int]:
        if day not in self._cache:
            session = self.calendar.session(day)
            starts = [] if session is None else self.calendar.expected_bar_starts(session, TIMEFRAME)
            self._cache[day] = {start: i for i, start in enumerate(starts)}
        return self._cache[day]

    def of(self, bar: BacktestBar) -> int | None:
        return self.slots(bar.session_date).get(bar.start_ts)


def validate_bars(instrument_id, bars, slot_index: SlotIndex) -> None:
    previous = None
    for i, bar in enumerate(bars):
        if not isinstance(bar, BacktestBar):
            raise BacktestRefused(RefusalCode.INVALID_DATA, f"bar {i} is not a BacktestBar", instrument_id)
        if slot_index.of(bar) is None:
            raise BacktestRefused(
                RefusalCode.INVALID_DATA, f"bar {bar.start_ts.isoformat()} is not an expected {TIMEFRAME} slot of a session", instrument_id
            )
        if previous is not None and bar.start_ts <= previous.start_ts:
            raise BacktestRefused(
                RefusalCode.INVALID_DATA, f"bars must be strictly increasing: {bar.start_ts.isoformat()} after {previous.start_ts.isoformat()}", instrument_id
            )
        previous = bar


def prepare_instrument(request: BacktestRequest, data: InstrumentData, calendar: TradingCalendar) -> PreparedInstrument:
    iid = data.instrument_id
    slot_index = SlotIndex(calendar)
    validate_bars(iid, data.bars, slot_index)

    refused = sorted(a.ex_date for a in data.corporate_actions if a.action_type in REFUSED_ACTION_TYPES and request.start <= a.ex_date <= request.end)
    if refused:
        raise BacktestRefused(
            RefusalCode.CORPORATE_ACTION_IN_PERIOD,
            f"a split/bonus ex-date falls inside the evaluation period ({', '.join(d.isoformat() for d in refused)}); decision D11",
            iid,
        )

    floor = max((a.ex_date for a in data.corporate_actions if a.action_type in REFUSED_ACTION_TYPES and a.ex_date <= request.start), default=None)
    prior_sessions = sorted({b.session_date for b in data.bars if b.session_date < request.start})
    period_sessions = sorted({b.session_date for b in data.bars if request.start <= b.session_date <= request.end})
    if not period_sessions:
        raise BacktestRefused(RefusalCode.NO_DATA, f"no bars between {request.start} and {request.end}", iid)

    w = request.warmup_sessions
    usable_prior = [d for d in prior_sessions if floor is None or d >= floor]
    used_prior = usable_prior[-w:] if w else []
    truncated = floor if w and len(used_prior) < min(w, len(prior_sessions)) else None
    shortfall = w - len(used_prior)
    shifted = period_sessions[:shortfall]
    evaluation = period_sessions[shortfall:]
    if not evaluation:
        raise BacktestRefused(
            RefusalCode.INSUFFICIENT_DATA,
            f"all {len(period_sessions)} session(s) with data are needed for the {w}-session warm-up; nothing left to evaluate",
            iid,
        )
    source = {
        (False, False): WarmupSource.NONE,
        (True, False): WarmupSource.PRIOR_SESSIONS,
        (False, True): WarmupSource.SHIFTED_INTO_PERIOD,
        (True, True): WarmupSource.PRIOR_AND_SHIFTED,
    }[(bool(used_prior), bool(shifted))]

    warmup_days, eval_days = set(used_prior) | set(shifted), set(evaluation)
    warmup_bars = tuple(b for b in data.bars if b.session_date in warmup_days)
    eval_bars = tuple(b for b in data.bars if b.session_date in eval_days)
    ignored = len(data.bars) - len(warmup_bars) - len(eval_bars)

    effective_start, effective_end = evaluation[0], evaluation[-1]
    present = {b.start_ts for b in eval_bars}
    expected, missing, without = 0, [], []
    for session in calendar.sessions_between(effective_start, request.end):
        starts = calendar.expected_bar_starts(session, TIMEFRAME)
        expected += len(starts)
        absent = [s for s in starts if s not in present]
        if len(absent) == len(starts):
            without.append(session.day)
        missing.extend(absent)
    coverage = (Decimal(len(eval_bars)) * 100 / expected).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) if expected else Decimal(0)
    disclosed = tuple(
        sorted(
            (a for a in data.corporate_actions if a.action_type not in REFUSED_ACTION_TYPES and request.start <= a.ex_date <= request.end),
            key=lambda a: (a.ex_date, a.action_type, a.detail),
        )
    )
    quality = DataQuality(
        requested_start=request.start,
        requested_end=request.end,
        effective_start=effective_start,
        effective_end=effective_end,
        warmup_sessions_requested=w,
        warmup_sessions_used=len(warmup_days),
        warmup_source=source,
        warmup_session_dates=tuple(sorted(warmup_days)),
        warmup_bars=len(warmup_bars),
        warmup_truncated_by_corporate_action=truncated,
        evaluation_sessions=len(evaluation),
        sessions_without_data=tuple(without),
        evaluation_bars=len(eval_bars),
        expected_bars=expected,
        missing_bars=len(missing),
        missing_bar_starts=tuple(missing[:MAX_LISTED_MISSING]),
        coverage_pct=coverage,
        bars_ignored_outside_window=ignored,
        corporate_actions_disclosed=disclosed,
    )
    return PreparedInstrument(iid, warmup_bars, eval_bars, data.corporate_actions, quality)
