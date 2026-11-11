"""Per-instrument, per-session selection with a session lock, and the decision record.

Lifecycle for one instrument (feed every finalized bar, in order, through `observe`):

  * Bars ending at or before the session's checkpoint (09:30 IST) are fed to the regime tracker.
  * The decision is made exactly once per session, at the checkpoint: right after the bar ending at 09:30, or, if
    that bar is missing, on the first later bar of the session, BEFORE that bar is fed. Either way it uses only
    bars ending at or before the checkpoint. The checkpoint is never postponed to wait for more data.
  * The decision is then locked for the rest of the session: later bars, ADX changes or repeated checkpoint calls
    return the same decision object. A NO_SELECTION is locked too (no mid-session retry). A new session starts a
    new decision. Each instrument has its own state.
  * If a session's data ends before its checkpoint, no decision is made for it (`decision()` returns None) and
    nothing can trade in that session.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.indicators.core import IndicatorBar
from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.strategies.base import BAR, SessionTracker
from app.strategies.selection.candidates import CandidateEvaluation
from app.strategies.selection.policy import Policy, PinnedPolicy, RegimePriorityPolicy, SelectionMode
from app.strategies.selection.regime import RegimeResult, RegimeTracker
from app.strategies.selection.selector import SelectionReason, SelectionStatus, WarmupContext, select


@dataclass(frozen=True)
class SelectionDecision:
    """The locked, explainable outcome of one instrument's selection for one session."""

    instrument_id: Any
    session_date: date
    mode: SelectionMode
    checkpoint: datetime
    decided_on_bar_start: datetime | None  # the bar whose arrival triggered the decision (None: explicit call)
    regime: RegimeResult
    status: SelectionStatus
    reason: SelectionReason
    selected: CandidateEvaluation | None
    candidates: tuple[CandidateEvaluation, ...]
    locked: bool = True

    @property
    def selected_config_id(self) -> str | None:
        return None if self.selected is None else self.selected.config_id

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready explanation (enums as values, datetimes as ISO-8601)."""

        def plain(value):
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            if isinstance(value, enum.Enum):
                return value.value
            if isinstance(value, tuple):
                return [plain(v) for v in value]
            if hasattr(value, "__dataclass_fields__"):
                return {k: plain(getattr(value, k)) for k in value.__dataclass_fields__}
            return value

        return plain(self)


class InstrumentSelector:
    def __init__(self, instrument_id: Any, policy: Policy, calendar: TradingCalendar | None = None) -> None:
        if not isinstance(policy, (RegimePriorityPolicy, PinnedPolicy)):
            raise ValueError(f"policy must be a RegimePriorityPolicy or PinnedPolicy, got {type(policy).__name__}")
        self.instrument_id = instrument_id
        self.policy = policy
        self.calendar = calendar or TradingCalendar()
        self._sessions = SessionTracker(self.calendar)
        self._regime = RegimeTracker(policy.regime)
        self._session_date: date | None = None
        self._session_bars: list[IndicatorBar] = []
        self._decisions: dict[date, SelectionDecision] = {}

    def checkpoint_at(self, session_date: date) -> datetime:
        return datetime.combine(session_date, self.policy.regime.checkpoint, tzinfo=EXCHANGE_TZ)

    def observe(self, bar: IndicatorBar) -> SelectionDecision | None:
        """Feed one bar. Returns the decision on the bar that triggers it, otherwise None."""
        new_session, _gap = self._sessions.observe(bar)
        if new_session:
            self._session_date, self._session_bars = bar.session_date, []
        checkpoint = self.checkpoint_at(bar.session_date)
        end = bar.start_ts + BAR
        decision = None
        if end > checkpoint and bar.session_date not in self._decisions:
            decision = self._decide(bar.session_date, bar.start_ts)  # before this bar: it ends after the checkpoint
        self._regime.update(bar)
        if end <= checkpoint:
            self._session_bars.append(bar)
        if end == checkpoint and bar.session_date not in self._decisions:  # never re-decide a locked session
            decision = self._decide(bar.session_date, bar.start_ts)
        return decision

    def checkpoint(self, session_date: date) -> SelectionDecision:
        """Decide now for the current session (e.g. the caller knows 09:30 has passed with no bar yet); idempotent:
        a session already decided returns its locked decision unchanged."""
        if session_date in self._decisions:
            return self._decisions[session_date]
        if session_date != self._session_date:
            raise ValueError(f"{session_date} is not the current session ({self._session_date}); a decision is only made live")
        return self._decide(session_date, None)

    def decision(self, session_date: date) -> SelectionDecision | None:
        return self._decisions.get(session_date)

    def entry_permitted(self, config_id: str, session_date: date) -> bool:
        """Only the locked, selected configuration may open positions in the session."""
        decision = self._decisions.get(session_date)
        return decision is not None and decision.status is SelectionStatus.SELECTED and decision.selected_config_id == config_id

    def _decide(self, session_date: date, trigger: datetime | None) -> SelectionDecision:
        checkpoint = self.checkpoint_at(session_date)
        regime = self._regime.snapshot(checkpoint, len(self._session_bars))
        context = WarmupContext(self._regime.bars_observed, tuple(self._session_bars), self.calendar)
        outcome = select(self.policy, regime, context)
        decision = SelectionDecision(
            self.instrument_id, session_date, self.policy.mode, checkpoint, trigger, regime,
            outcome.status, outcome.reason, outcome.selected, outcome.candidates,
        )
        self._decisions[session_date] = decision
        return decision


class SelectionBook:
    """Independent selectors for several instruments (one policy each). No shared state between instruments."""

    def __init__(self, policies: dict[Any, Policy], calendar: TradingCalendar | None = None) -> None:
        if not policies:
            raise ValueError("at least one instrument policy is required")
        self._selectors = {instrument: InstrumentSelector(instrument, policy, calendar) for instrument, policy in policies.items()}

    def selector(self, instrument_id: Any) -> InstrumentSelector:
        try:
            return self._selectors[instrument_id]
        except KeyError:
            raise ValueError(f"no selection policy for instrument {instrument_id!r}") from None

    def observe(self, instrument_id: Any, bar: IndicatorBar) -> SelectionDecision | None:
        return self.selector(instrument_id).observe(bar)

    def decision(self, instrument_id: Any, session_date: date) -> SelectionDecision | None:
        return self.selector(instrument_id).decision(session_date)

    def entry_permitted(self, instrument_id: Any, config_id: str, session_date: date) -> bool:
        return self.selector(instrument_id).entry_permitted(config_id, session_date)
