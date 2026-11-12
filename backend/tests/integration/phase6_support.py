"""Fixtures for Phase 6 tests: real trading records produced by the real Phase 4/5 services.

Every candle is a SYNTHETIC fixture (see agent_support / trading_support); execution is enabled only inside these
tests, never in development configuration.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.enums import AgentDecisionKind
from app.db.models import AgentDecision, Order
from app.main import create_app
from app.marketdata.providers.registry import ProviderRegistry
from tests.integration.agent_support import IST, World


def round_trip(w: World) -> World:
    """The Phase 5 ORB paper lifecycle: entry signal -> risk-sized BUY -> fill -> 15:15 square-off -> SELL fill."""
    w.opening_range()
    w.at("09:30")
    w.configure()
    w.start()
    w.bar("09:30", "100.5", "102.5", "100.4", "102")
    w.at("09:35")
    w.cycle()
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    w.flat_bars("09:45", "15:10")
    w.at("15:15")
    w.cycle()
    w.flat_bars("15:15", "15:20")
    w.at("15:25")
    w.cycle()
    w.db.expire_all()
    return w


def entry_decision(w: World) -> AgentDecision:
    return w.db.scalar(select(AgentDecision).where(AgentDecision.kind == AgentDecisionKind.EVALUATION,
                                                   AgentDecision.action == "ENTER_LONG"))


def orders(w: World) -> list[Order]:
    return list(w.db.scalars(select(Order).order_by(Order.created_at)))


def hhmm(ts) -> str:
    return ts.astimezone(IST).strftime("%H:%M")


def client_for(settings, clock, provider=None) -> TestClient:
    app = create_app(settings, ProviderRegistry({}), clock)
    if provider is not None:
        app.state.llm_provider = provider
    return TestClient(app)


def headers(user_id, settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, settings)}"}


class FakeProvider:
    """A deterministic stand-in for an LLM: answers with a narrative built only from given text, or fails."""

    name, model, handles_retries = "fake", "fake-1", False

    def __init__(self, narrative: str = "This is a paper-trading record explained from the verified facts.", error=None):
        self.narrative, self.error, self.calls = narrative, error, 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return json.dumps({"narrative": self.narrative, "caveats": ["Paper trading only."]})
