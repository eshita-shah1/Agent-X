"""Phase 5 agent API: authentication, lifecycle transitions, configuration versions, the risk-settings lock, the kill
switch and user isolation. The application runs with its default configuration (execution disabled), so a started
agent is SIGNALS_ONLY and says so."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.enums import InstrumentType, TradingStatus
from app.db.models import AgentInstance, User
from app.main import create_app
from app.marketdata.providers.registry import ProviderRegistry
from tests.conftest import auth_headers, login, register_user
from tests.integration.agent_support import ist
from tests.integration.trading_support import FIXTURE_PROVIDER, Clock, make_instrument, make_provider

DAY = date(2026, 1, 5)
RISK = {"max_risk_per_trade_pct": "1", "max_position_allocation_pct": "20", "max_portfolio_exposure_pct": "100",
        "max_daily_loss_pct": "5", "max_drawdown_pct": "10", "max_position_quantity": None, "resize_allowed": True}


@pytest.fixture
def api(test_settings, db):
    make_provider(db)
    clock = Clock(ist(DAY, "10:00"))
    settings = test_settings.model_copy(update={"marketdata_provider": FIXTURE_PROVIDER})
    with TestClient(create_app(settings, ProviderRegistry({}), clock)) as c:
        c.clock = clock
        yield c


@pytest.fixture
def instrument(db):
    return make_instrument(db)


def user(api) -> dict:
    info = register_user(api)
    return auth_headers(login(api, info["email"]))


def config(instrument_id: int, **over) -> dict:
    body = {"instrument_ids": [instrument_id], "mode": "PINNED",
            "candidates": [{"config_id": "orb", "strategy_id": "ORB", "params": {}}]}
    body.update(over)
    return body


def test_every_endpoint_requires_authentication(api):
    for method, path in [("get", "status"), ("get", "config"), ("put", "config"), ("post", "start"), ("post", "pause"),
                         ("post", "resume"), ("post", "stop"), ("post", "kill-switch"), ("get", "decisions"),
                         ("get", "events"), ("get", "cycles")]:
        assert getattr(api, method)(f"/api/v1/agent/{path}").status_code == 401, path


def test_status_before_configuration(api):
    h = user(api)
    body = api.get("/api/v1/agent/status", headers=h).json()
    assert body["configured"] is False and body["entries_allowed"] is False
    assert api.get("/api/v1/agent/config", headers=h).status_code == 404
    assert api.post("/api/v1/agent/start", headers=h).status_code == 404


def test_configuration_versions(api, instrument):
    h = user(api)
    r = api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    assert r.status_code == 201 and r.json()["config"]["version"] == 1 and r.json()["created"] is True
    assert r.json()["config"]["warmup_sessions"] == 2  # the backtest default (D16)
    again = api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["config"]["version"] == 1
    assert api.get("/api/v1/agent/config", headers=h).json()["version"] == 1


@pytest.mark.parametrize("body,detail", [
    ({"candidates": [{"config_id": "x", "strategy_id": "NOPE", "params": {}}]}, "unknown strategy"),
    ({"candidates": [{"config_id": "x", "strategy_id": "ORB", "params": {"range_minutes": 7}}]}, "multiple of 5"),
    ({"mode": "REGIME_PRIORITY"}, "priority"),
])
def test_invalid_configurations_are_refused(api, instrument, body, detail):
    h = user(api)
    r = api.put("/api/v1/agent/config", headers=h, json=config(instrument.id, **body))
    assert r.status_code == 422 and detail in r.text


def test_disabled_and_future_instruments_are_refused(api, db):
    future = make_instrument(db, "NIFTYFUT", instrument_type=InstrumentType.FUTURE, trading_status=TradingStatus.DISABLED,
                             disabled_reason="futures disabled")
    h = user(api)
    r = api.put("/api/v1/agent/config", headers=h, json=config(future.id))
    assert r.status_code == 422 and "only equities" in r.text


def test_start_with_execution_disabled_is_signals_only_and_says_so(api, instrument):
    h = user(api)
    api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    r = api.post("/api/v1/agent/start", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "RUNNING" and body["mode"] == "SIGNALS_ONLY" and body["entries_allowed"] is False
    assert body["execution_enabled"] is False
    assert any(b.startswith("EXECUTION_DISABLED") for b in body["blockers"])
    assert any("never submits an order" in d for d in body["disclosures"])


def test_lifecycle_transitions_and_invalid_ones(api, instrument):
    h = user(api)
    api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    assert api.post("/api/v1/agent/pause", headers=h).status_code == 409  # STOPPED cannot pause
    assert api.post("/api/v1/agent/resume", headers=h).status_code == 409
    assert api.post("/api/v1/agent/start", headers=h).json()["status"] == "RUNNING"
    assert api.post("/api/v1/agent/start", headers=h).status_code == 409  # already running
    assert api.post("/api/v1/agent/pause", headers=h).json()["status"] == "PAUSED"
    assert api.post("/api/v1/agent/resume", headers=h).json()["status"] == "RUNNING"
    assert api.post("/api/v1/agent/stop", headers=h).json()["status"] == "STOPPED"
    assert api.post("/api/v1/agent/stop", headers=h).status_code == 409
    types = [e["event_type"] for e in api.get("/api/v1/agent/events", headers=h).json()]
    for t in ("agent.created", "agent.config_saved", "agent.started", "agent.paused", "agent.resumed", "agent.stopped"):
        assert t in types


def test_configuration_and_risk_settings_are_locked_while_running(api, instrument):
    h = user(api)
    assert api.put("/api/v1/risk/settings", headers=h, json=RISK).status_code == 200
    api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    api.post("/api/v1/agent/start", headers=h)
    r = api.put("/api/v1/risk/settings", headers=h, json=RISK)
    assert r.status_code == 409 and "AGENT_RUNNING" in r.text
    assert api.put("/api/v1/agent/config", headers=h, json=config(instrument.id, warmup_sessions=3)).status_code == 409
    api.post("/api/v1/agent/pause", headers=h)
    assert api.put("/api/v1/risk/settings", headers=h, json=RISK).status_code == 200
    r = api.put("/api/v1/agent/config", headers=h, json=config(instrument.id, warmup_sessions=3))
    assert r.status_code == 201
    assert r.json()["config"]["effective_from_session"] == "2026-01-06"  # the next session (D12)
    status = api.get("/api/v1/agent/status", headers=h).json()
    assert status["effective_config"]["version"] == 1 and status["pending_config"]["version"] == 2


def test_kill_switch_pauses_and_release_does_not_resume(api, instrument):
    h = user(api)
    api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    api.post("/api/v1/agent/start", headers=h)
    body = api.post("/api/v1/agent/kill-switch", headers=h, json={"reason": "drill"}).json()
    assert body["status"] == "PAUSED" and body["kill_switch_engaged"] is True
    assert api.get("/api/v1/risk/state", headers=h).json()["kill_switch_engaged"] is True  # one switch (D11)
    r = api.post("/api/v1/agent/resume", headers=h)
    assert r.status_code == 409 and "KILL_SWITCH_ENGAGED" in r.text
    api.post("/api/v1/risk/kill-switch", headers=h, json={"engaged": False, "reason": "drill over"})
    assert api.get("/api/v1/agent/status", headers=h).json()["status"] == "PAUSED"
    assert api.post("/api/v1/agent/resume", headers=h).json()["status"] == "RUNNING"


def test_users_are_isolated(api, instrument, db):
    a, b = user(api), user(api)
    api.put("/api/v1/agent/config", headers=a, json=config(instrument.id))
    api.post("/api/v1/agent/start", headers=a)
    assert api.get("/api/v1/agent/status", headers=b).json()["configured"] is False
    for path in ("decisions", "events", "cycles"):
        assert api.get(f"/api/v1/agent/{path}", headers=b).json() == []
    assert api.post("/api/v1/agent/stop", headers=b).status_code == 404
    assert api.get("/api/v1/agent/status", headers=a).json()["status"] == "RUNNING"
    # B configures its own agent; A's is untouched
    api.put("/api/v1/agent/config", headers=b, json=config(instrument.id, warmup_sessions=1))
    owners = {x.user_id for x in db.scalars(select(AgentInstance))}
    assert len(owners) == 2
    assert api.get("/api/v1/agent/config", headers=a).json()["warmup_sessions"] == 2


def test_client_supplied_user_ids_are_ignored(api, instrument, db):
    a, b = user(api), user(api)
    b_id = str(db.scalars(select(User).order_by(User.created_at)).all()[-1].id)
    body = config(instrument.id)
    r = api.put("/api/v1/agent/config", headers=a, json={**body, "user_id": b_id})
    assert r.status_code == 422  # unknown fields are refused outright
    r = api.put(f"/api/v1/agent/config?user_id={b_id}", headers=a, json=body)
    assert r.status_code == 201
    assert api.get("/api/v1/agent/status", headers=b).json()["configured"] is False


def test_one_agent_per_account_is_a_database_rule(api, instrument, db):
    from sqlalchemy.exc import IntegrityError

    h = user(api)
    api.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
    agent = db.scalar(select(AgentInstance))
    db.add(AgentInstance(user_id=agent.user_id, account_id=agent.account_id, status_changed_at=agent.status_changed_at))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_start_is_refused_when_the_market_data_provider_is_not_registered(test_settings, db, instrument):
    clock = Clock(ist(DAY, "10:00"))
    settings = test_settings.model_copy(update={"marketdata_provider": "unregistered"})
    with TestClient(create_app(settings, ProviderRegistry({}), clock)) as c:
        h = user(c)
        c.put("/api/v1/agent/config", headers=h, json=config(instrument.id))
        r = c.post("/api/v1/agent/start", headers=h)
        assert r.status_code == 409 and "MARKET_DATA_UNAVAILABLE" in r.text
        assert c.get("/api/v1/agent/status", headers=h).json()["status"] == "STOPPED"
