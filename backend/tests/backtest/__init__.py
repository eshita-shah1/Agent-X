"""Shared helpers for the backtest tests.

`Scripted` is a test-only strategy that enters and exits exactly when told (keyed by session date and bar start
"HH:MM" IST), so every fill rule can be checked against hand-computed numbers. It inherits the common strategy rules
(square-off, cooldown, cut-off, gap suppression) from the real base class, and is injected through the engine's
strategy factory; production code always uses the registry.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.backtest import BacktestBar, BacktestRequest, CapitalSource, CostModel, InstrumentData, run_backtest
from app.marketdata.calendar import EXCHANGE_TZ
from app.strategies import Family, ORBConfig, ProtectiveLevels, Reason, Strategy
from app.strategies.selection import Candidate, PinnedPolicy
from tests.indicators import ist
from tests.strategies import DAY, NEXT_DAY, THIRD_DAY, slot_of

D = Decimal
CONFIG = ORBConfig(entry_cutoff=time(14, 45))
CANDIDATE = Candidate("s-1", "ORB", CONFIG)
POLICY = PinnedPolicy(CANDIDATE)
ALL_SLOTS = [f"{(9 * 60 + 15 + 5 * i) // 60:02d}:{(9 * 60 + 15 + 5 * i) % 60:02d}" for i in range(75)]


def bar(day: date, hhmm: str, o, h=None, low=None, c=None, volume=1000, source_ref=None) -> BacktestBar:
    """The bar starting at `hhmm` IST. With only `o`, a flat bar (o = h = l = c)."""
    o = D(str(o))
    h = o if h is None else D(str(h))
    low = o if low is None else D(str(low))
    c = o if c is None else D(str(c))
    hour, minute = map(int, hhmm.split(":"))
    return BacktestBar(ist(day, hour, minute), day, o, h, low, c, volume, source_ref)


def session(day: date, price="100", overrides=None, skip=(), until=None) -> list[BacktestBar]:
    """A full session of flat bars at `price`; `overrides` maps "HH:MM" -> (o, h, l, c); `skip` drops slots;
    `until` keeps only bars starting at or before that time."""
    overrides = overrides or {}
    out = []
    for hhmm in ALL_SLOTS:
        if hhmm in skip or (until is not None and hhmm > until):
            continue
        out.append(bar(day, hhmm, *overrides[hhmm]) if hhmm in overrides else bar(day, hhmm, price))
    return out


class Scripted(Strategy):
    strategy_id = "ORB"
    family = Family.OPENING_BREAKOUT
    config_type = ORBConfig

    def __init__(self, config, calendar, entries, exits) -> None:
        super().__init__(config, calendar)
        self.entries, self.exits = entries, exits

    @staticmethod
    def key(b):
        return b.session_date, b.start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M")

    def update_indicators(self, b, new_session):
        return {}

    def entry(self, b, values):
        if self.key(b) in self.entries:
            return Reason.ORB_BREAKOUT, self.entries[self.key(b)], True
        return Reason.NO_ENTRY_CONDITION, None, False

    def exit_reason(self, b, values):
        return Reason.VWAP_CROSS_DOWN if self.key(b) in self.exits else None


def scripted(entries=None, exits=()):
    """A strategy factory; `entries` maps "HH:MM" (or (date, "HH:MM")) -> ProtectiveLevels | None."""
    def norm(k):
        return k if isinstance(k, tuple) else (NEXT_DAY, k)

    e = {norm(k): v for k, v in (entries or {}).items()}
    x = {norm(k) for k in exits}
    return lambda candidate, calendar: Scripted(candidate.config, calendar, e, x)


def run(bars, entries=None, exits=(), *, capital="100000", costs=None, warmup=0, start=NEXT_DAY, end=NEXT_DAY, actions=(), policy=POLICY):
    request = BacktestRequest(
        ("X",), start, end, policy, D(capital), CapitalSource.ACCOUNT_INITIAL_CAPITAL, costs or CostModel(), warmup,
    )
    result = run_backtest(request, {"X": InstrumentData("X", tuple(bars), tuple(actions))}, strategy_factory=scripted(entries, exits))
    return result.instruments[0]


def local(ts: datetime) -> str:
    return ts.astimezone(EXCHANGE_TZ).strftime("%H:%M")


def levels(**kw) -> ProtectiveLevels:
    return ProtectiveLevels(**kw)


__all__ = [
    "ALL_SLOTS", "CANDIDATE", "CONFIG", "D", "DAY", "NEXT_DAY", "POLICY", "THIRD_DAY", "Scripted", "bar", "levels", "local",
    "run", "scripted", "session", "slot_of", "timedelta",
]
