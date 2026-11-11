"""Deterministic fixtures for the Phase 5 agent integration tests.

Every candle here is a SYNTHETIC test fixture labelled UNVERIFIED_PROVIDER_NATIVE (as Yahoo data is archived) and
priced by hand so that the approved ORB strategy (pinned) gives a known signal: a 15-minute opening range of 99-101,
then a close above 101. The cost schedule is the synthetic test fixture. Execution is enabled ONLY inside these tests
(`simulation_settings`); development configuration is never changed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agent import configuration, lifecycle
from app.agent.cycle import run_cycle
from app.core.config import Settings
from app.db.models import AgentInstance, Candle
from app.marketdata.providers.registry import ProviderRegistry
from app.marketdata.service import MarketDataService
from app.orders.service import TradingContext
from tests.integration.trading_support import (
    FIXTURE_PROVIDER,
    Clock,
    configure_risk,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    simulation_settings,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 1, 5)  # a Monday
NEXT_DAY = date(2026, 1, 6)
PINNED_ORB = {"mode": "PINNED", "candidates": [{"config_id": "orb-default", "strategy_id": "ORB", "params": {}}]}


def ist(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).astimezone(UTC)


def agent_settings(base: Settings, execution: bool = True, **over) -> Settings:
    if execution:
        return simulation_settings(base, marketdata_provider=FIXTURE_PROVIDER, **over)
    return base.model_copy(update={"marketdata_provider": FIXTURE_PROVIDER, **over})


class World:
    """One account, one instrument, the fixture provider and a movable clock."""

    def __init__(self, db, engine, base_settings: Settings, execution: bool = True, capital: str = "100000.00",
                 day: date = DAY, risk: dict | None = None, **settings_over):
        self.db, self.engine = db, engine
        self.factory = sessionmaker(bind=engine, expire_on_commit=False)
        self.settings = agent_settings(base_settings, execution, **settings_over)
        self.provider = make_provider(db)
        self.instrument = make_instrument(db)
        self.account = make_account(db, capital)
        if risk is not False:
            configure_risk(db, self.account, **(risk or {}))
        self.day = day
        self.clock = Clock(ist(day, "09:15"))
        self.registry = ProviderRegistry({})

    # ---- data -------------------------------------------------------------------------------------------------

    def bar(self, hhmm: str, o: str, h: str, low: str, c: str, day: date | None = None, delay_seconds: int = 0) -> Candle:
        """Archive the 5-minute bar starting at hhmm (IST), observed `delay_seconds` after it ends."""
        start = ist(day or self.day, hhmm)
        return make_candle(self.db, self.provider, self.instrument, c, end=start + timedelta(minutes=5), open=o, high=h,
                           low=low, verified=False, observed_delay=timedelta(seconds=delay_seconds))

    def opening_range(self, day: date | None = None) -> None:
        for hhmm in ("09:15", "09:20", "09:25"):
            self.bar(hhmm, "100", "101", "99", "100", day)

    def flat_bars(self, first: str, last: str, price: str = "102", day: date | None = None) -> None:
        t, end = ist(day or self.day, first), ist(day or self.day, last)
        while t <= end:
            hhmm = t.astimezone(IST).strftime("%H:%M")
            self.bar(hhmm, price, str(float(price) + 0.2), str(float(price) - 0.2), price, day)
            t += timedelta(minutes=5)

    def at(self, hhmm: str, day: date | None = None, seconds: int = 30) -> None:
        self.clock.now = ist(day or self.day, hhmm) + timedelta(seconds=seconds)

    # ---- agent ------------------------------------------------------------------------------------------------

    def svc(self, s=None) -> MarketDataService:
        return MarketDataService(s or self.db, self.registry, self.settings, self.clock)

    def configure(self, config: dict | None = None, instrument_ids: list[int] | None = None, warmup_sessions: int = 0):
        c = config or PINNED_ORB
        data = configuration.AgentConfigInput(instrument_ids or [self.instrument.id], c["mode"], c["candidates"], warmup_sessions)
        result = lifecycle.configure(self.db, self.account.user_id, data, self.svc().calendar, self.clock())
        self.db.commit()
        return result

    def start(self):
        ctx = TradingContext.build(self.db, self.settings, self.clock)
        agent = lifecycle.start(self.db, self.settings, ctx, self.svc(), self.account.user_id, self.clock())
        self.db.commit()
        return agent

    def ctx(self):
        return TradingContext.build(self.db, self.settings, self.clock)

    def cycle(self, worker: str = "test-worker") -> dict:
        self.db.expire_all()
        result = run_cycle(self.engine, self.factory, self.settings, self.clock, self.agent().id, worker, self.registry)
        self.db.expire_all()
        return result

    def agent(self) -> AgentInstance:
        self.db.expire_all()
        return self.db.scalar(select(AgentInstance).where(AgentInstance.user_id == self.account.user_id))
