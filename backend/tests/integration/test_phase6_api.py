"""Phase 6 API on PostgreSQL: authentication, user isolation, journal CRUD/search/reflections, explanations."""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.db.models import JournalEntry, JournalReflection
from app.llm.providers import ProviderError
from tests.integration.agent_support import DAY, World
from tests.integration.phase6_support import FakeProvider, client_for, entry_decision, headers, orders, round_trip
from tests.integration.trading_support import make_account

ROUTES = [
    ("get", "/api/v1/explanations/provider"), ("get", "/api/v1/explanations/daily-summary"),
    ("get", "/api/v1/explanations/decisions/1"), ("get", "/api/v1/explanations/events/1"),
    ("get", "/api/v1/explanations/orders/00000000-0000-0000-0000-000000000000"),
    ("post", "/api/v1/explanations/generate"), ("get", "/api/v1/explanations"), ("get", "/api/v1/explanations/1"),
    ("get", "/api/v1/journal"), ("post", "/api/v1/journal"), ("get", "/api/v1/journal/overview"),
    ("get", "/api/v1/journal/tags"), ("post", "/api/v1/journal/sync"), ("get", "/api/v1/journal/analytics"),
    ("post", "/api/v1/journal/analytics/narrative"), ("get", "/api/v1/journal/1"), ("patch", "/api/v1/journal/1"),
    ("delete", "/api/v1/journal/1"), ("post", "/api/v1/journal/1/reflection"),
    ("post", "/api/v1/journal/1/reflection/assist"),
]


@pytest.fixture
def traded(db, engine, test_settings):
    w = round_trip(World(db, engine, test_settings))
    w.at("16:00")
    return w


@pytest.fixture
def api(traded):
    with client_for(traded.settings, traded.clock) as c:
        yield c


def h(w):
    return headers(w.account.user_id, w.settings)


def synced(api, w) -> dict:
    r = api.post("/api/v1/journal/sync", json={"date_from": DAY.isoformat(), "date_to": DAY.isoformat()}, headers=h(w))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("method, path", ROUTES)
def test_every_phase6_route_requires_authentication(api, method, path):
    assert getattr(api, method)(path).status_code == 401


def test_sync_then_browse_search_and_filter(api, traded):
    w = traded
    assert synced(api, w)["created"] == 5
    assert synced(api, w)["created"] == 0  # idempotent
    page = api.get("/api/v1/journal", headers=h(w)).json()
    assert page["total"] == 5 and all(i["entry_type"] == "AUTO" for i in page["items"])
    trade = api.get("/api/v1/journal", params={"source_event": "TRADE_CLOSED"}, headers=h(w)).json()["items"][0]
    assert trade["symbol"] == "TESTCO" and trade["outcome"] in ("WIN", "LOSS", "BREAKEVEN")
    by_outcome = api.get("/api/v1/journal", params={"outcome": trade["outcome"]}, headers=h(w)).json()
    assert [i["id"] for i in by_outcome["items"]] == [trade["id"]]
    paged = api.get("/api/v1/journal", params={"limit": 2, "offset": 2, "sort": "occurred_at", "direction": "asc"},
                    headers=h(w)).json()
    assert paged["total"] == 5 and len(paged["items"]) == 2 and paged["offset"] == 2
    assert api.get("/api/v1/journal", params={"date_from": "2026-01-06"}, headers=h(w)).json()["total"] == 0
    assert api.get("/api/v1/journal", params={"date_to": DAY.isoformat()}, headers=h(w)).json()["total"] == 5
    assert api.get("/api/v1/journal", params={"limit": 101}, headers=h(w)).status_code == 422
    sell = orders(w)[1]
    linked = api.get("/api/v1/journal", params={"order_id": str(sell.id)}, headers=h(w)).json()
    assert {i["source_event"] for i in linked["items"]} == {"TRADE_CLOSED"}

    detail = api.get(f"/api/v1/journal/{trade['id']}", headers=h(w)).json()
    assert detail["linked"]["trade"]["status"] == "CLOSED" and len(detail["linked"]["fills"]) == 2
    assert detail["reflection_allowed"] is True and detail["linked"]["order"]["status"] == "FILLED"

    r = api.patch(f"/api/v1/journal/{trade['id']}", json={"notes": "Clean breakout; patience at square-off.",
                                                         "tags": ["breakout", "Patience"]}, headers=h(w))
    assert r.status_code == 200 and r.json()["tags"] == ["breakout", "patience"]
    assert r.json()["title"] == trade["title"] and r.json()["outcome"] == trade["outcome"]  # facts preserved
    found = api.get("/api/v1/journal", params={"q": "patience"}, headers=h(w)).json()
    assert [i["id"] for i in found["items"]] == [trade["id"]]
    assert api.get("/api/v1/journal", params={"q": "breakout patience", "sort": "relevance"}, headers=h(w)).json()["total"] == 1
    assert api.get("/api/v1/journal", params={"tag": "breakout"}, headers=h(w)).json()["total"] == 1
    assert api.get("/api/v1/journal", params={"tag": ["breakout", "missing"]}, headers=h(w)).json()["total"] == 0
    assert api.get("/api/v1/journal/tags", headers=h(w)).json() == [{"name": "breakout", "entries": 1},
                                                                    {"name": "patience", "entries": 1}]
    overview = api.get("/api/v1/journal/overview", headers=h(w)).json()
    assert overview["total"] == 5 and overview["automatic"] == 5 and overview["manual"] == 0


def test_automatic_entries_keep_their_facts_and_cannot_be_deleted(api, traded):
    w = traded
    synced(api, w)
    entry = api.get("/api/v1/journal", params={"source_event": "TRADE_CLOSED"}, headers=h(w)).json()["items"][0]
    r = api.patch(f"/api/v1/journal/{entry['id']}", json={"title": "rewritten"}, headers=h(w))
    assert r.status_code == 409 and "AUTO_ENTRY_FACTS_IMMUTABLE" in r.text
    r = api.patch(f"/api/v1/journal/{entry['id']}", json={"order_id": None}, headers=h(w))
    assert r.status_code == 409
    assert api.delete(f"/api/v1/journal/{entry['id']}", headers=h(w)).status_code == 409


def test_manual_entry_lifecycle(api, traded):
    w = traded
    sell = orders(w)[1]
    r = api.post("/api/v1/journal", json={
        "title": "Why I trusted the ORB", "order_id": str(sell.id), "entry_thesis": "Range was tight",
        "lessons": "Let the square-off work", "rating": 4, "tags": ["orb"]}, headers=h(w))
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["entry_type"] == "MANUAL" and e["instrument_id"] == w.instrument.id and e["outcome"] != "NONE"
    assert e["linked"]["trade"]["status"] == "CLOSED"
    r = api.patch(f"/api/v1/journal/{e['id']}", json={"title": "Why I trusted the opening range", "rating": 5}, headers=h(w))
    assert r.status_code == 200 and r.json()["title"] == "Why I trusted the opening range" and r.json()["tags"] == ["orb"]
    assert api.post("/api/v1/journal", json={"title": "  "}, headers=h(w)).status_code == 422
    assert api.post("/api/v1/journal", json={"title": "x", "rating": 9}, headers=h(w)).status_code == 422
    assert api.post("/api/v1/journal", json={"title": "x", "user_id": "y"}, headers=h(w)).status_code == 422
    assert api.post("/api/v1/journal", json={"title": "x", "notes": "n" * 10_001}, headers=h(w)).status_code == 422
    assert api.post("/api/v1/journal", json={"title": "x", "tags": ["bad tag!"]}, headers=h(w)).status_code == 422
    assert api.post("/api/v1/journal", json={"title": "x", "agent_decision_id": 999999}, headers=h(w)).status_code == 422

    r = api.post(f"/api/v1/journal/{e['id']}/reflection", json={
        "original_thesis": "Range breakout", "followed_strategy_rules": True, "risk_limits_respected": True,
        "exit_followed_strategy": True, "mistakes": "Hesitated", "lessons": "Trust the plan"}, headers=h(w))
    assert r.status_code == 201 and r.json()["followed_strategy_rules"] is True
    r = api.post(f"/api/v1/journal/{e['id']}/reflection", json={"lessons": "Updated"}, headers=h(w))
    assert r.status_code == 200 and r.json()["lessons"] == "Updated" and r.json()["mistakes"] is None
    assert api.get("/api/v1/journal", params={"has_reflection": True}, headers=h(w)).json()["total"] == 1

    loose = api.post("/api/v1/journal", json={"title": "Thoughts on the market"}, headers=h(w)).json()
    r = api.post(f"/api/v1/journal/{loose['id']}/reflection", json={"lessons": "x"}, headers=h(w))
    assert r.status_code == 409 and "TRADE_NOT_CLOSED" in r.text

    assert api.delete(f"/api/v1/journal/{e['id']}", headers=h(w)).status_code == 204
    assert api.get(f"/api/v1/journal/{e['id']}", headers=h(w)).status_code == 404
    assert w.db.scalar(select(func.count()).select_from(JournalReflection)) == 0


def test_reflection_assist_suggests_but_never_saves(traded):
    w = traded
    fake = FakeProvider("The TESTCO trade opened on the breakout and was closed by the agent at square-off.")
    with client_for(w.settings, w.clock, fake) as api:
        synced(api, w)
        entry = api.get("/api/v1/journal", params={"source_event": "TRADE_CLOSED"}, headers=h(w)).json()["items"][0]
        r = api.post(f"/api/v1/journal/{entry['id']}/reflection/assist", headers=h(w))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["saved"] is False and len(body["questions"]) >= 5
        assert body["draft"]["status"] == "GENERATED" and "AI-generated" in body["draft"]["label"]
        assert w.db.scalar(select(func.count()).select_from(JournalReflection)) == 0
    with client_for(w.settings, w.clock) as api:
        r = api.post(f"/api/v1/journal/{entry['id']}/reflection/assist", headers=h(w))
        assert r.json()["draft"]["status"] == "DISABLED" and r.json()["questions"]


def test_users_never_see_or_link_each_others_records(api, traded):
    w = traded
    synced(api, w)
    mine = api.get("/api/v1/journal", headers=h(w)).json()["items"][0]
    d = entry_decision(w)
    gen = api.post("/api/v1/explanations/generate", json={"subject_type": "AGENT_DECISION", "subject_id": str(d.id)},
                   headers=h(w)).json()
    other = make_account(w.db)
    oh = headers(other.user_id, w.settings)
    sell = orders(w)[1]
    assert api.get("/api/v1/journal", headers=oh).json()["total"] == 0
    assert api.get(f"/api/v1/journal/{mine['id']}", headers=oh).status_code == 404
    assert api.patch(f"/api/v1/journal/{mine['id']}", json={"notes": "x"}, headers=oh).status_code == 404
    assert api.delete(f"/api/v1/journal/{mine['id']}", headers=oh).status_code == 404
    assert api.post(f"/api/v1/journal/{mine['id']}/reflection", json={}, headers=oh).status_code == 404
    r = api.post("/api/v1/journal", json={"title": "steal", "order_id": str(sell.id)}, headers=oh)
    assert r.status_code == 422 and "LINK_NOT_FOUND" in r.text
    assert api.get(f"/api/v1/explanations/decisions/{d.id}", headers=oh).status_code == 404
    assert api.get(f"/api/v1/explanations/orders/{sell.id}", headers=oh).status_code == 404
    assert api.get(f"/api/v1/explanations/{gen['id']}", headers=oh).status_code == 404
    assert api.get("/api/v1/explanations", headers=oh).json() == []
    r = api.post("/api/v1/explanations/generate", json={"subject_type": "ORDER", "subject_id": str(sell.id)}, headers=oh)
    assert r.status_code == 404
    summary = api.get("/api/v1/explanations/daily-summary", params={"session_date": DAY.isoformat()}, headers=oh).json()
    assert summary["verified_facts"]["orders"]["total"] == 0 and summary["verified_facts"]["fills"] == 0
    assert api.get("/api/v1/journal/analytics", headers=oh).json()["analytics"]["closed_trades"]["total"] == 0
    assert w.db.scalar(select(func.count()).select_from(JournalEntry).where(JournalEntry.user_id == other.user_id)) == 0


def test_explanation_endpoints_preview_generate_and_label(traded):
    w = traded
    d = entry_decision(w)
    with client_for(w.settings, w.clock) as api:
        status = api.get("/api/v1/explanations/provider", headers=h(w)).json()
        assert status["enabled"] is False and status["fallback"] == "rule_based"
        preview = api.get(f"/api/v1/explanations/decisions/{d.id}", headers=h(w)).json()
        assert preview["persisted"] is False and preview["id"] is None and preview["narrative"] is None
        layers = [s["layer"] for s in preview["deterministic"]["sections"]]
        assert layers[:2] == ["Selection", "Strategy signal"] and "Risk decision" in layers and "Order" in layers
        r = api.post("/api/v1/explanations/generate", json={"subject_type": "AGENT_DECISION", "subject_id": str(d.id)},
                     headers=h(w))
        assert r.status_code == 201 and r.json()["narrative"]["status"] == "DISABLED"
        again = api.post("/api/v1/explanations/generate", json={"subject_type": "AGENT_DECISION", "subject_id": str(d.id)},
                         headers=h(w))
        assert again.status_code == 200 and again.json()["id"] == r.json()["id"]
        assert api.get("/api/v1/explanations/decisions/999999", headers=h(w)).status_code == 404
        assert api.get("/api/v1/explanations/orders/not-a-uuid", headers=h(w)).status_code == 404
        r = api.post("/api/v1/explanations/generate", json={"subject_type": "DAILY_SUMMARY", "subject_id": "2026-13-45"},
                     headers=h(w))
        assert r.status_code == 404
    fake = FakeProvider("The agent recorded an ORB entry signal for TESTCO and submitted an entry order.")
    with client_for(w.settings, w.clock, fake) as api:
        r = api.post("/api/v1/explanations/generate", json={"subject_type": "AGENT_DECISION", "subject_id": str(d.id)},
                     headers=h(w))
        n = r.json()["narrative"]
        assert r.status_code == 201 and n["status"] == "GENERATED" and n["provider"] == "fake" and n["model"] == "fake-1"
        assert "not a verified trading fact" in n["label"]
        listed = api.get("/api/v1/explanations", params={"subject_type": "AGENT_DECISION"}, headers=h(w)).json()
        assert len(listed) == 2
    with client_for(w.settings, w.clock, FakeProvider(error=ProviderError("TIMEOUT"))) as api:
        sell = orders(w)[1]
        r = api.post("/api/v1/explanations/generate", json={"subject_type": "ORDER", "subject_id": str(sell.id)},
                     headers=h(w))
        body = r.json()
        assert r.status_code == 201 and body["narrative"]["status"] == "FAILED" and body["deterministic"]["text"]
        assert body["evidence"]["order_id"] == str(sell.id) and len(body["evidence"]["fill_ids"]) == 1
    with client_for(w.settings, w.clock, FakeProvider("The trade earned 123456.78 rupees.")) as api:
        r = api.post("/api/v1/explanations/generate", json={"subject_type": "DAILY_SUMMARY", "subject_id": DAY.isoformat()},
                     headers=h(w))
        assert r.json()["narrative"]["status"] == "INVALID_OUTPUT" and r.json()["narrative"]["text"] is None


def test_daily_summary_and_analytics_endpoints(api, traded):
    w = traded
    synced(api, w)
    s = api.get("/api/v1/explanations/daily-summary", params={"session_date": DAY.isoformat()}, headers=h(w)).json()
    assert s["verified_facts"]["closed_trades"]["total"] == 1 and s["verified_facts"]["orders"]["total"] == 2
    assert s["subject_key"] == DAY.isoformat()
    a = api.get("/api/v1/journal/analytics", params={"date_from": "2026-01-01", "date_to": "2026-01-10"}, headers=h(w)).json()
    an = a["analytics"]
    assert an["closed_trades"]["total"] == 1 and "ORB" in an["by_strategy"]
    assert any("Fewer than 30" in c for c in an["caveats"]) and any("not causes" in c for c in an["caveats"])
    assert any(s["layer"] == "Caveat" for s in a["deterministic"]["sections"])
    r = api.get("/api/v1/journal/analytics", params={"date_from": "2025-01-01", "date_to": "2026-06-01"}, headers=h(w))
    assert r.status_code == 422  # window bounded
    r = api.post("/api/v1/journal/analytics/narrative", json={"date_from": "2026-01-01", "date_to": "2026-01-10"}, headers=h(w))
    assert r.status_code == 201 and r.json()["subject_type"] == "JOURNAL_INSIGHTS"
    assert r.json()["narrative"]["status"] == "DISABLED"
    assert api.post("/api/v1/explanations/generate", json={"subject_type": "JOURNAL_INSIGHTS", "subject_id": "x"},
                    headers=h(w)).status_code == 422


def test_sync_window_is_bounded(api, traded):
    w = traded
    r = api.post("/api/v1/journal/sync", json={"date_from": "2025-01-01", "date_to": "2026-01-05"}, headers=h(w))
    assert r.status_code == 422
    r = api.post("/api/v1/journal/sync", json={"date_from": "2026-01-06", "date_to": "2026-01-05"}, headers=h(w))
    assert r.status_code == 422
    assert api.post("/api/v1/journal/sync", headers=h(w)).json()["date_to"] == date(2026, 1, 5).isoformat()
