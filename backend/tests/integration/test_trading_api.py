"""Phase 4 HTTP API: authentication, validation, isolation and end-to-end order flow."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import CashLedgerEntry, Fill, Order, Position, RiskDecision
from app.main import create_app
from tests.conftest import auth_headers, login, register_user
from tests.integration.trading_support import NOW, Clock, enabled_settings, make_candle, make_instrument, make_provider

RISK = {
    "max_risk_per_trade_pct": "1",
    "max_position_allocation_pct": "20",
    "max_portfolio_exposure_pct": "100",
    "max_daily_loss_pct": "5",
    "max_drawdown_pct": "10",
    "max_position_quantity": None,
    "resize_allowed": True,
}


def order_body(instrument_id: int, **over) -> dict:
    body = {
        "client_order_id": "ord-1",
        "instrument_id": instrument_id,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10,
        "stop_loss_price": "95",
    }
    body.update(over)
    return body


@pytest.fixture
def market(db):
    provider = make_provider(db)
    inst = make_instrument(db)
    make_candle(db, provider, inst, "100")
    return provider, inst


def _client(settings) -> TestClient:
    app = create_app(settings)
    app.state.clock = Clock()
    return TestClient(app)


@pytest.fixture
def live(test_settings):
    """Client with execution explicitly enabled against the synthetic fixture provider."""
    with _client(enabled_settings(test_settings)) as c:
        yield c


def _user(c: TestClient, capital: str = "100000.00", risk: bool = True) -> dict:
    info = register_user(c, capital)
    h = auth_headers(login(c, info["email"]))
    if risk:
        assert c.put("/api/v1/risk/settings", json=RISK, headers=h).status_code == 200
    return h


# ---- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/v1/orders"),
        ("get", "/api/v1/orders"),
        ("get", "/api/v1/portfolio"),
        ("get", "/api/v1/portfolio/fills"),
        ("get", "/api/v1/risk/settings"),
        ("put", "/api/v1/risk/settings"),
        ("get", "/api/v1/risk/state"),
        ("post", "/api/v1/risk/kill-switch"),
        ("get", "/api/v1/risk/decisions"),
        ("get", "/api/v1/exchange/status"),
    ],
)
def test_endpoints_require_authentication(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ---- default (blocked) configuration --------------------------------------------------------


def test_exchange_status_reports_fills_blocked_by_default(client, make_user):
    _, h = make_user()
    body = client.get("/api/v1/exchange/status", headers=h).json()
    assert body["execution_enabled"] is False
    assert body["execution_price_source"] == "disabled"
    assert any(r.startswith("SOURCE_DISABLED") for r in body["blocking_reasons"])
    assert "COSTS_NOT_CONFIGURED" in body["blocking_reasons"]


def test_orders_are_rejected_not_filled_under_default_configuration(client, market):
    h = _user(client)
    resp = client.post("/api/v1/orders", json=order_body(market[1].id), headers=h)
    assert resp.status_code == 201
    order = resp.json()["order"]
    assert order["status"] == "REJECTED" and "SOURCE_DISABLED" in order["reject_reason"]
    assert resp.json()["risk_decision"]["price_check"]["rejection"] == "SOURCE_DISABLED"


# ---- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"quantity": 0},
        {"quantity": -5},
        {"quantity": 1.5},
        {"side": "SHORT"},
        {"order_type": "STOP"},
        {"order_type": "LIMIT"},  # missing limit price
        {"limit_price": "101"},  # limit price on a market order
        {"stop_loss_price": "-1"},
        {"stop_loss_price": "1.0000001"},  # more than 6 dp
        {"client_order_id": ""},
        {"client_order_id": "has space"},
        {"strategy_max_risk_pct": "101"},
        {"side": "SELL", "stop_loss_price": "95"},
    ],
)
def test_invalid_order_requests_are_rejected(live, market, over):
    h = _user(live)
    assert live.post("/api/v1/orders", json=order_body(market[1].id, **over), headers=h).status_code == 422


def test_unknown_instrument_is_422(live, market):
    h = _user(live)
    resp = live.post("/api/v1/orders", json=order_body(999_999), headers=h)
    assert resp.status_code == 422 and "UNKNOWN_INSTRUMENT" in resp.json()["detail"]


@pytest.mark.parametrize(
    "over", [{"max_risk_per_trade_pct": "0"}, {"max_drawdown_pct": "100.5"}, {"resize_allowed": None}]
)
def test_invalid_risk_settings(live, over):
    h = _user(live, risk=False)
    assert live.put("/api/v1/risk/settings", json={**RISK, **over}, headers=h).status_code == 422


def test_risk_settings_have_no_defaults(live):
    h = _user(live, risk=False)
    assert live.get("/api/v1/risk/settings", headers=h).status_code == 404
    partial = {k: v for k, v in RISK.items() if k != "max_position_quantity"}
    assert live.put("/api/v1/risk/settings", json=partial, headers=h).status_code == 422


# ---- end-to-end ------------------------------------------------------------------------------


def test_buy_fill_portfolio_and_history(live, market, db):
    h = _user(live)
    resp = live.post("/api/v1/orders", json=order_body(market[1].id), headers=h)
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["status"] == "FILLED" and order["filled_quantity"] == 10
    assert Decimal(order["avg_fill_price"]) == Decimal("100.05")

    detail = live.get(f"/api/v1/orders/{order['id']}", headers=h).json()
    assert len(detail["fills"]) == 1
    fill = detail["fills"][0]
    assert fill["provider_code"] == "fixture" and fill["price_basis"] == "AS_TRADED_VERIFIED"
    assert detail["risk_decision"]["outcome"] == "APPROVED"

    p = live.get("/api/v1/portfolio", headers=h).json()
    assert Decimal(p["cash_balance"]) == Decimal("98978.4995")
    assert p["valuation_complete"] is True
    assert Decimal(p["equity"]) == Decimal("98978.4995") + Decimal("1000")
    assert Decimal(p["unrealized_pnl"]) == Decimal("-0.5")
    assert p["positions"][0]["quantity"] == 10

    assert len(live.get("/api/v1/portfolio/fills", headers=h).json()) == 1
    assert len(live.get("/api/v1/risk/decisions", headers=h).json()) == 1
    assert [o["id"] for o in live.get("/api/v1/orders?status=FILLED", headers=h).json()] == [order["id"]]


def test_portfolio_reports_unknown_equity_when_marks_are_stale(live, market):
    h = _user(live)
    live.post("/api/v1/orders", json=order_body(market[1].id), headers=h)
    live.app.state.clock.now = NOW + timedelta(hours=2)
    p = live.get("/api/v1/portfolio", headers=h).json()
    assert p["equity"] is None and p["valuation_complete"] is False
    assert p["positions"][0]["unrealized_pnl"] is None
    assert p["positions"][0]["mark_unavailable_reason"].startswith("STALE")


def test_idempotent_replay_and_conflict(live, market, db):
    h = _user(live)
    first = live.post("/api/v1/orders", json=order_body(market[1].id), headers=h)
    again = live.post("/api/v1/orders", json=order_body(market[1].id), headers=h)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["replayed"] is True and again.json()["order"]["id"] == first.json()["order"]["id"]
    conflict = live.post("/api/v1/orders", json=order_body(market[1].id, quantity=11), headers=h)
    assert conflict.status_code == 409
    assert db.scalar(select(func.count()).select_from(Fill)) == 1


def test_cancel_flow(live, market):
    h = _user(live)
    resp = live.post(
        "/api/v1/orders",
        json=order_body(market[1].id, order_type="LIMIT", limit_price="99", stop_loss_price="90"),
        headers=h,
    )
    order = resp.json()["order"]
    assert order["status"] == "PENDING" and Decimal(order["reserved_cash"]) > 0
    cancelled = live.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "changed mind"}, headers=h)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert Decimal(cancelled.json()["reserved_cash"]) == 0
    assert live.post(f"/api/v1/orders/{order['id']}/cancel", headers=h).status_code == 409


def test_kill_switch_via_api(live, market):
    h = _user(live)
    live.post(
        "/api/v1/orders",
        json=order_body(market[1].id, client_order_id="rest", order_type="LIMIT", limit_price="99", stop_loss_price="90"),
        headers=h,
    )
    r = live.post("/api/v1/risk/kill-switch", json={"engaged": True, "reason": "test"}, headers=h)
    assert r.status_code == 200 and r.json()["kill_switch_engaged"] is True
    assert live.get("/api/v1/orders?status=PENDING", headers=h).json() == []
    blocked = live.post("/api/v1/orders", json=order_body(market[1].id, client_order_id="after"), headers=h).json()
    assert "KILL_SWITCH_ENGAGED" in blocked["order"]["reject_reason"]
    released = live.post("/api/v1/risk/kill-switch", json={"engaged": False, "reason": "resolved"}, headers=h)
    assert released.json()["kill_switch_engaged"] is False
    ok = live.post("/api/v1/orders", json=order_body(market[1].id, client_order_id="again"), headers=h).json()
    assert ok["order"]["status"] == "FILLED"


# ---- isolation ------------------------------------------------------------------------------


def test_users_cannot_read_or_cancel_each_others_orders(live, market):
    ha = _user(live)
    hb = _user(live)
    order = live.post(
        "/api/v1/orders",
        json=order_body(market[1].id, order_type="LIMIT", limit_price="99", stop_loss_price="90"),
        headers=ha,
    ).json()["order"]
    assert live.get(f"/api/v1/orders/{order['id']}", headers=hb).status_code == 404
    assert live.post(f"/api/v1/orders/{order['id']}/cancel", headers=hb).status_code == 404
    assert live.get("/api/v1/orders", headers=hb).json() == []
    assert live.get("/api/v1/risk/decisions", headers=hb).json() == []
    # the same client_order_id is independent per user
    other = live.post(
        "/api/v1/orders",
        json=order_body(market[1].id, order_type="LIMIT", limit_price="99", stop_loss_price="90"),
        headers=hb,
    )
    assert other.status_code == 201 and other.json()["order"]["id"] != order["id"]


def test_client_supplied_user_id_in_order_body_is_ignored(live, market, db):
    ha = _user(live)
    victim = register_user(live)
    body = order_body(market[1].id, user_id=victim["user"]["id"], account_id=victim["account"]["id"])
    resp = live.post("/api/v1/orders", json=body, headers=ha)
    assert resp.status_code == 201
    stored = db.scalar(select(Order))
    assert str(stored.user_id) != victim["user"]["id"]


# ---- transaction rollback through the API ------------------------------------------------------


def test_failure_mid_execution_leaves_no_partial_state(test_settings, market, db, monkeypatch):
    from app.exchange import service as exchange_service

    with _client(enabled_settings(test_settings)) as c:
        h = _user(c)

        def boom(*a, **k):
            raise RuntimeError("injected position failure")

        monkeypatch.setattr(exchange_service, "apply_buy", boom)
        c2 = TestClient(c.app, raise_server_exceptions=False)
        assert c2.post("/api/v1/orders", json=order_body(market[1].id), headers=h).status_code == 500
    for model in (Order, Fill, CashLedgerEntry, Position, RiskDecision):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__
