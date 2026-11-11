"""Deterministic replay of the approved selector and strategies over the archive.

Every cycle rebuilds the session's selection and each candidate strategy from scratch: the warm-up sessions (fixed for
the whole session by an anchor stored on first use; the backtest's warm-up rule, decision D16, including its
corporate-action floor, decision D13) are fed with FLAT positions, as backtest warm-up is, then today's finalized bars
in time order. The locked selection's configuration receives the position context derived from the fills; every other
candidate receives FLAT and only keeps its indicators warm. Nothing outlives the cycle, so a restart, a second worker or
a replay of the same data produces exactly the same signals.

Bars come only from `MarketDataService.get_candles` (canonical rows of the active provider), restricted to bars that
had ended and been observed by the cycle's clock: no forming candle and no future observation is ever used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CorporateAction
from app.indicators.core import IndicatorBar
from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.marketdata.service import MarketDataService
from app.strategies.base import FLAT, PositionContext, Signal
from app.strategies.configs import corporate_action_floor, lookback_days, warmup_sessions_before
from app.strategies.registry import get_definition
from app.strategies.selection import InstrumentSelector, PinnedPolicy, SelectionDecision, SelectionStatus

TIMEFRAME = "5m"
PAGE = 1000


class ReplayError(ValueError):
    """The archive cannot be replayed for this instrument (e.g. a bar off the session grid, a non-final bar)."""


@dataclass(frozen=True)
class ArchivedBar:
    bar: IndicatorBar
    candle_id: int
    observation_version: int
    provider: str
    price_basis: str
    close: Decimal  # the archived close, exactly (the strategies see it as float)

    @property
    def start(self) -> datetime:
        return self.bar.start_ts


@dataclass(frozen=True)
class BarOutcome:
    archived: ArchivedBar
    acting_config_id: str | None  # the session's locked selection, once decided
    signal: Signal | None  # the acting configuration's signal for this bar


@dataclass(frozen=True)
class ReplayResult:
    session_date: date
    replay_from_session: date | None
    warmup_truncated_by: date | None
    warmup_bars: int
    outcomes: tuple[BarOutcome, ...]  # today's bars, oldest first
    decision: SelectionDecision | None
    candidates: dict  # config_id -> Candidate


def load_bars(svc: MarketDataService, instrument_id: int, start: datetime, now: datetime) -> list[ArchivedBar]:
    out: list[ArchivedBar] = []
    rows, nxt = svc.get_candles(instrument_id=instrument_id, timeframe=TIMEFRAME, start=start, end=now, limit=PAGE)
    pages = [rows]
    while nxt is not None:  # bounded: the cursor only moves forward through a finite archive window
        rows, nxt = svc.get_candles(instrument_id=instrument_id, timeframe=TIMEFRAME, start=start, end=now, limit=PAGE, after=nxt)
        pages.append(rows)
    for rows in pages:
        for r in rows:
            if r["bar_end_ts"] > now or r["observed_at"] > now:
                continue  # not yet available at the cycle's decision time
            if not r["is_final"]:
                raise ReplayError(f"candle {r['candle_id']} is not final")
            try:
                bar = IndicatorBar(
                    r["bar_start_ts"].astimezone(UTC), r["session_date"], float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["volume"]),
                )
            except ValueError as exc:
                raise ReplayError(f"candle {r['candle_id']}: {exc}") from None
            out.append(ArchivedBar(bar, r["candle_id"], r["observation_version"], r["provider"], r["price_basis"], r["close"]))
    return out


def warmup_anchor(
    db: Session, svc: MarketDataService, instrument_id: int, session_date: date, warmup_sessions: int, now: datetime
) -> tuple[date | None, date | None]:
    """(first warm-up session, the corporate-action floor when it truncated the warm-up) for a session."""
    start = datetime.combine(session_date - timedelta(days=lookback_days(warmup_sessions)), time(0), tzinfo=EXCHANGE_TZ)
    bars = load_bars(svc, instrument_id, start, min(now, datetime.combine(session_date, time(0), tzinfo=EXCHANGE_TZ)))
    prior = sorted({b.bar.session_date for b in bars if b.bar.session_date < session_date})
    actions = [(a.ex_date, a.action_type.value) for a in db.scalars(select(CorporateAction).where(CorporateAction.instrument_id == instrument_id))]
    used, truncated = warmup_sessions_before(prior, corporate_action_floor(actions, session_date), warmup_sessions)
    return (used[0] if used else None), truncated


def replay(
    policy,
    calendar: TradingCalendar,
    instrument_id: int,
    warmup: list[ArchivedBar],
    today: list[ArchivedBar],
    contexts: dict[datetime, PositionContext],
    session_date: date,
) -> tuple[tuple[BarOutcome, ...], SelectionDecision | None, dict]:
    candidates = (policy.candidate,) if isinstance(policy, PinnedPolicy) else policy.ordered()
    strategies = {c.config_id: get_definition(c.strategy_id).create(c.config, calendar) for c in candidates}
    selector = InstrumentSelector(instrument_id, policy, calendar)
    try:
        for ab in warmup:
            selector.observe(ab.bar)
            for strategy in strategies.values():
                strategy.evaluate(ab.bar, FLAT)
        outcomes = []
        for ab in today:
            selector.observe(ab.bar)
            decision = selector.decision(session_date)
            acting = decision.selected_config_id if decision is not None and decision.status is SelectionStatus.SELECTED else None
            signal = None
            for config_id, strategy in strategies.items():
                result = strategy.evaluate(ab.bar, contexts.get(ab.start, FLAT) if config_id == acting else FLAT)
                if config_id == acting:
                    signal = result
            outcomes.append(BarOutcome(ab, acting, signal))
    except ValueError as exc:
        raise ReplayError(str(exc)) from None
    return tuple(outcomes), selector.decision(session_date), {c.config_id: c for c in candidates}


def run_replay(
    db: Session,
    svc: MarketDataService,
    policy,
    instrument_id: int,
    session_date: date,
    replay_from_session: date | None,
    contexts_for,
    now: datetime,
) -> ReplayResult:
    """Load the anchored warm-up and today's bars and replay them. `contexts_for(bar_starts)` returns the acting
    configuration's position context for today's bars (derived from fills by the caller)."""
    day_start = datetime.combine(session_date, time(0), tzinfo=EXCHANGE_TZ)
    warmup = []
    if replay_from_session is not None:
        warmup = [
            b for b in load_bars(svc, instrument_id, datetime.combine(replay_from_session, time(0), tzinfo=EXCHANGE_TZ), min(now, day_start))
            if b.bar.session_date < session_date
        ]
    today = [b for b in load_bars(svc, instrument_id, day_start, now) if b.bar.session_date == session_date]
    outcomes, decision, candidates = replay(
        policy, svc.calendar, instrument_id, warmup, today, contexts_for([b.start for b in today]), session_date
    )
    return ReplayResult(session_date, replay_from_session, None, len(warmup), outcomes, decision, candidates)
