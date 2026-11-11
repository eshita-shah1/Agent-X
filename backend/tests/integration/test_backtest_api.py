"""Backtest API, background runner, persistence, reproducibility and user isolation (Phase 3, Step 4B).

TestClient runs FastAPI background tasks before returning the response, so a 202 POST has already executed the run
by the time the test reads it back.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.backtest import run_backtest
from app.backtest_runs import jobs
from app.backtest_runs.jobs import create_run, execute_run, load_data
from app.backtest_runs.requests import build_request
from app.backtest.model import CapitalSource
from app.db.enums import BacktestRunStatus
from app.db.models import (
    BacktestEquityPoint,
    BacktestEvent,
    BacktestInstrumentResult,
    BacktestRun,
    BacktestSelectionDecision,
    BacktestTrade,
    Candle,
    User,
    VirtualAccount,
)
from app.marketdata.service import load_calendar
from tests.integration.backtest_support import archive, body, disabled_future, instrument, split

RESULT_MODELS = (BacktestInstrumentResult, BacktestSelectionDecision, BacktestTrade, BacktestEvent, BacktestEquityPoint)
API = "/api/v1/backtests"


@pytest.fixture
def inst(db):
    i = instrument(db)
    archive(db, i)
    return i


@pytest.fixture
def alice(make_market_user):
    return make_market_user()


@pytest.fixture
def bob(make_market_user):
    return make_market_user()


def start(client, headers, payload):
    return client.post(API, json=payload, headers=headers)


def finished(client, headers, payload):
    resp = start(client, headers, payload)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "QUEUED"
    run = client.get(f"{API}/{resp.json()['id']}", headers=headers)
    assert run.status_code == 200
    return run.json()


def counts(db):
    return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (BacktestRun, *RESULT_MODELS)}


# ---- happy path ------------------------------------------------------------------------------------------------------


def test_a_backtest_runs_in_the_background_and_stores_complete_results(market_client, alice, inst, db):
    _, h = alice
    run = finished(market_client, h, body([inst.id]))
    assert run["status"] == "SUCCEEDED", run
    assert (run["capital"], run["capital_source"], run["cost_basis"], run["price_basis"]) == (
        "100000.00", "ACCOUNT_INITIAL_CAPITAL", "GROSS_OF_COSTS", "UNVERIFIED_PROVIDER_NATIVE",
    )
    assert (run["fill_model"], run["sizing_model"], run["engine_version"]) == ("NEXT_BAR_OPEN_V1", "WHOLE_SHARES_FULL_ALLOCATION_V1", "1")
    assert all(len(run[k]) == 64 for k in ("config_fingerprint", "data_fingerprint", "result_fingerprint"))
    assert any(d.startswith("PER_INSTRUMENT") for d in run["disclosures"]) and any(d.startswith("STATISTICS") for d in run["disclosures"])
    assert run["summary"]["per_instrument_not_portfolio"] is True

    rid, iid = run["id"], inst.id
    (res,) = market_client.get(f"{API}/{rid}/instruments", headers=h).json()["items"]
    q = res["data_quality"]
    assert (q["effective_start"], q["effective_end"], q["warmup_source"], q["evaluation_bars"]) == ("2026-11-04", "2026-11-10", "PRIOR_SESSIONS", 375)
    assert res["benchmark_status"] in ("VALID", "LIMITED") and res["benchmark"]["notes"]

    equity = market_client.get(f"{API}/{rid}/instruments/{iid}/equity", headers=h, params={"limit": 5000}).json()
    assert equity["count"] == 375 and len(equity["items"]) == 375
    assert all(Decimal(p["equity"]) == Decimal(p["cash"]) + p["quantity"] * Decimal(p["close"]) for p in equity["items"])
    selections = market_client.get(f"{API}/{rid}/instruments/{iid}/selections", headers=h).json()
    assert selections["count"] == 5 and selections["items"][0]["decision"]["checkpoint"] == "2026-11-04T04:00:00Z"  # 09:30 IST
    trades = market_client.get(f"{API}/{rid}/instruments/{iid}/trades", headers=h).json()
    events = market_client.get(f"{API}/{rid}/instruments/{iid}/events", headers=h).json()
    assert trades["count"] >= 1 and events["count"] >= trades["count"]
    assert {"entry_reference_price", "entry_price", "exit_reason", "net_pnl"} <= set(trades["items"][0])


def test_stored_results_reproduce_an_independent_engine_run(market_client, alice, inst, db, md_settings):
    _, h = alice
    run = finished(market_client, h, body([inst.id]))
    stored = db.get(BacktestRun, uuid.UUID(run["id"]))
    request = build_request(stored.request["submitted"], stored.capital, stored.capital_source)
    direct = run_backtest(request, load_data(db, md_settings, request), load_calendar(db))
    assert (direct.config_fingerprint, direct.data_fingerprint, direct.result_fingerprint) == (
        run["config_fingerprint"], run["data_fingerprint"], run["result_fingerprint"],
    )
    # the stored rows are the engine's values exactly (unconstrained NUMERIC)
    rows = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == stored.id).order_by(BacktestTrade.seq)).all()
    assert [(r.entry_price, r.exit_price, r.net_pnl, r.stop_level) for r in rows] == [
        (t.entry_price, t.exit_price, t.net_pnl, t.stop_level) for t in direct.instruments[0].trades
    ]
    again = finished(market_client, h, body([inst.id]))
    assert again["result_fingerprint"] == run["result_fingerprint"] and again["id"] != run["id"]


def test_loaded_bars_and_fingerprints_do_not_depend_on_the_connection_time_zone(alice, inst, md_settings, clock, engine, db):
    """Regression: timestamptz comes back in the session time zone; the loader normalises to UTC so equal instants
    give identical selection explanations and therefore identical result fingerprints."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    run = queued(db, alice[0], inst.id, md_settings, clock)
    request = build_request(run.request["submitted"], run.capital, run.capital_source)
    prints = set()
    for zone in ("UTC", "Asia/Kolkata", "America/New_York"):
        eng = create_engine(engine.url, connect_args={"options": f"-c timezone={zone}"})
        with Session(eng) as s:
            data = load_data(s, md_settings, request)
            assert all(b.start_ts.utcoffset().total_seconds() == 0 for d in data.values() for b in d.bars)
            r = run_backtest(request, data, load_calendar(s))
            prints.add((r.data_fingerprint, r.result_fingerprint))
        eng.dispose()
    assert len(prints) == 1


def test_capital_override_and_modelled_costs(market_client, alice, inst):
    _, h = alice
    run = finished(market_client, h, body([inst.id], capital="25000.50", cost_model={"fee_per_order": "20", "slippage_bps": "5"}))
    assert run["status"] == "SUCCEEDED"
    assert (run["capital"], run["capital_source"], run["cost_basis"]) == ("25000.50", "REQUEST_OVERRIDE", "NET_OF_MODELLED_COSTS")


def test_each_instrument_is_simulated_independently_with_the_full_capital(market_client, alice, db):
    _, h = alice
    a, b = instrument(db, "AAA"), instrument(db, "BBB")
    archive(db, a)
    archive(db, b, phase=1.0)
    run = finished(market_client, h, body([a.id, b.id]))
    items = market_client.get(f"{API}/{run['id']}/instruments", headers=h).json()["items"]
    assert [i["instrument_id"] for i in items] == [a.id, b.id]
    assert all(i["summary"]["starting_capital"] == "100000" for i in items)


def test_a_pinned_run(market_client, alice, inst):
    _, h = alice
    payload = body([inst.id], mode="PINNED", candidates=[{"config_id": "ema-x", "strategy_id": "EMA_CROSSOVER", "params": {"fast_period": 5, "entry_cutoff": "14:30"}}])
    run = finished(market_client, h, payload)
    assert run["status"] == "SUCCEEDED" and run["selection_mode"] == "PINNED"
    assert run["request"]["config"]["policy"]["candidates"][0]["params"]["fast_period"] == 5


# ---- refusals (HTTP 422 before anything is queued) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "over,code",
    [
        ({"end": "2026-11-12"}, "INVALID_PERIOD"),  # in the future (clock: 2026-11-11)
        ({"start": "2026-11-10", "end": "2026-11-04"}, "INVALID_PERIOD"),
        ({"start": "2025-01-01", "end": "2026-11-10"}, "INVALID_PERIOD"),  # longer than a year
        ({"start": "2026-10-01", "end": "2026-10-20"}, "NO_DATA"),
        ({"instrument_ids": [999999]}, "INVALID_CONFIGURATION"),
        ({"candidates": [{"config_id": "x", "strategy_id": "MACD", "priority": 1}]}, "INVALID_CONFIGURATION"),
        ({"candidates": [{"config_id": "x", "strategy_id": "ORB", "priority": 1, "params": {"nope": 1}}]}, "INVALID_CONFIGURATION"),
        ({"candidates": [{"config_id": "x", "strategy_id": "ORB", "priority": 1, "params": {"range_minutes": "15"}}]}, "INVALID_CONFIGURATION"),
        ({"candidates": [{"config_id": "x", "strategy_id": "ORB", "priority": 1, "params": {"range_minutes": 7}}]}, "INVALID_CONFIGURATION"),
        ({"candidates": [{"config_id": "x", "strategy_id": "ORB"}]}, "INVALID_CONFIGURATION"),  # regime mode needs a priority
        ({"candidates": [{"config_id": "x", "strategy_id": "ORB", "priority": 1}, {"config_id": "y", "strategy_id": "VWAP", "priority": 1}]}, "INVALID_CONFIGURATION"),
        ({"mode": "PINNED"}, "INVALID_CONFIGURATION"),  # five candidates in pinned mode
        ({"warmup_sessions": 5, "start": "2026-11-02", "end": "2026-11-06"}, "INSUFFICIENT_DATA"),
    ],
)
def test_invalid_requests_are_refused_with_422_and_nothing_is_queued(market_client, alice, inst, db, over, code):
    _, h = alice
    resp = start(market_client, h, body([inst.id], **over))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == code
    assert counts(db)["backtest_runs"] == 0


def test_a_split_in_the_period_is_refused(market_client, alice, inst, db):
    split(db, inst, __import__("datetime").date(2026, 11, 6))
    resp = start(market_client, alice[1], body([inst.id]))
    assert resp.status_code == 422 and resp.json()["detail"] == {
        "code": "CORPORATE_ACTION_IN_PERIOD", "message": resp.json()["detail"]["message"], "instrument_id": inst.id,
    }


def test_disabled_futures_are_refused(market_client, alice, inst, db):
    fut = disabled_future(db)
    resp = start(market_client, alice[1], body([inst.id, fut.id]))
    assert resp.status_code == 422 and resp.json()["detail"]["instrument_id"] == fut.id


@pytest.mark.parametrize(
    "over",
    [{"surprise": 1}, {"instrument_ids": []}, {"instrument_ids": [1, 1]}, {"capital": "0"}, {"capital": "1.234"}, {"mode": "BEST"},
     {"warmup_sessions": 21}, {"cost_model": {"slippage_bps": "-1"}}, {"cost_model": {"commission": "1"}}],
)
def test_malformed_bodies_are_rejected(market_client, alice, inst, db, over):
    resp = start(market_client, alice[1], body([inst.id], **over))
    assert resp.status_code == 422
    assert counts(db)["backtest_runs"] == 0


# ---- one active run per user; background-job failure handling ---------------------------------------------------


def queued(db, user_info, inst_id, md_settings, clock):
    user = db.get(User, uuid.UUID(user_info["user"]["id"]))
    submitted = body([inst_id], warmup_sessions=2, cost_model={"fee_per_order": "0", "fee_bps_per_side": "0", "sell_side_bps": "0", "slippage_bps": "0"})
    request = build_request(submitted, Decimal("100000.00"), CapitalSource.ACCOUNT_INITIAL_CAPITAL)
    return create_run(db, user, submitted, request, md_settings, clock)


def test_one_active_run_per_user_and_stale_runs_are_interrupted(market_client, alice, bob, inst, db, md_settings, clock):
    (info, h), (_, hb) = alice, bob
    active = queued(db, info, inst.id, md_settings, clock)
    resp = start(market_client, h, body([inst.id]))
    assert resp.status_code == 409 and resp.json()["detail"]["active_run_id"] == str(active.id)
    assert finished(market_client, hb, body([inst.id]))["status"] == "SUCCEEDED"  # other users are unaffected
    clock.advance(seconds=md_settings.backtest_job_heartbeat_timeout_seconds + 1)
    assert finished(market_client, h, body([inst.id]))["status"] == "SUCCEEDED"
    db.expire_all()
    stale = db.get(BacktestRun, active.id)
    assert (stale.status, stale.error_code) == (BacktestRunStatus.INTERRUPTED, "INTERRUPTED")


def test_a_refusal_at_run_time_fails_the_run_and_stores_nothing(alice, inst, db, md_settings, clock, engine):
    run = queued(db, alice[0], inst.id, md_settings, clock)
    split(db, inst, __import__("datetime").date(2026, 11, 5))  # the data changed after acceptance
    execute_run(_factory(engine), md_settings, run.id, clock)
    db.expire_all()
    stored = db.get(BacktestRun, run.id)
    assert (stored.status, stored.error_code, stored.result_fingerprint) == (BacktestRunStatus.FAILED, "CORPORATE_ACTION_IN_PERIOD", None)
    assert all(v == 0 for k, v in counts(db).items() if k != "backtest_runs")


def test_a_crash_fails_the_run_and_stores_nothing(alice, inst, db, md_settings, clock, engine, monkeypatch):
    run = queued(db, alice[0], inst.id, md_settings, clock)
    monkeypatch.setattr(jobs, "run_backtest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    execute_run(_factory(engine), md_settings, run.id, clock)
    db.expire_all()
    stored = db.get(BacktestRun, run.id)
    assert (stored.status, stored.error_code) == (BacktestRunStatus.FAILED, "INTERNAL_ERROR") and "boom" in stored.error_message
    assert all(v == 0 for k, v in counts(db).items() if k != "backtest_runs")


def test_a_run_interrupted_while_computing_stores_nothing(alice, inst, db, md_settings, clock, engine, monkeypatch):
    run = queued(db, alice[0], inst.id, md_settings, clock)
    factory = _factory(engine)
    real = jobs.run_backtest

    def interrupted(*a, **k):
        result = real(*a, **k)
        with factory() as s:
            s.get(BacktestRun, run.id).status = BacktestRunStatus.INTERRUPTED
            s.commit()
        return result

    monkeypatch.setattr(jobs, "run_backtest", interrupted)
    execute_run(factory, md_settings, run.id, clock)
    db.expire_all()
    assert db.get(BacktestRun, run.id).status is BacktestRunStatus.INTERRUPTED
    assert all(v == 0 for k, v in counts(db).items() if k != "backtest_runs")


def test_execute_only_runs_queued_runs(alice, inst, db, md_settings, clock, engine):
    run = queued(db, alice[0], inst.id, md_settings, clock)
    factory = _factory(engine)
    execute_run(factory, md_settings, run.id, clock)
    db.expire_all()
    first = db.get(BacktestRun, run.id).result_fingerprint
    execute_run(factory, md_settings, run.id, clock)  # a repeated hand-off does nothing
    execute_run(factory, md_settings, uuid.uuid4(), clock)
    db.expire_all()
    assert db.get(BacktestRun, run.id).result_fingerprint == first
    assert counts(db)["backtest_runs"] == 1


def _factory(engine):
    from app.db.session import make_sessionmaker

    return make_sessionmaker(engine)


# ---- isolation, authentication, read-only API ---------------------------------------------------------------------


def test_users_only_ever_see_their_own_runs_and_results(market_client, alice, bob, inst):
    (_, ha), (_, hb) = alice, bob
    run = finished(market_client, ha, body([inst.id]))
    rid = run["id"]
    for path in ("", "/instruments", f"/instruments/{inst.id}/trades", f"/instruments/{inst.id}/equity",
                 f"/instruments/{inst.id}/selections", f"/instruments/{inst.id}/events"):
        assert market_client.get(f"{API}/{rid}{path}", headers=hb).status_code == 404, path
        assert market_client.get(f"{API}/{rid}{path}", headers=ha).status_code == 200, path
    assert market_client.get(API, headers=hb).json()["count"] == 0
    assert market_client.get(API, headers=ha).json()["count"] == 1
    assert market_client.get(f"{API}/{uuid.uuid4()}", headers=ha).status_code == 404
    assert market_client.get(f"{API}/{rid}/instruments/{inst.id + 1}/trades", headers=ha).status_code == 404


@pytest.mark.parametrize(
    "method,path",
    [("get", API), ("post", API), ("get", f"{API}/strategies"), ("get", f"{API}/{uuid.uuid4()}"),
     ("get", f"{API}/{uuid.uuid4()}/instruments"), ("get", f"{API}/{uuid.uuid4()}/instruments/1/equity")],
)
def test_every_backtest_endpoint_requires_authentication(market_client, method, path):
    assert getattr(market_client, method)(path).status_code == 401
    assert getattr(market_client, method)(path, headers={"Authorization": "Bearer forged"}).status_code == 401


def test_results_cannot_be_changed_or_deleted_through_the_api(market_client, alice, inst):
    _, h = alice
    rid = finished(market_client, h, body([inst.id]))["id"]
    for method in ("put", "patch", "delete"):
        assert getattr(market_client, method)(f"{API}/{rid}", headers=h).status_code == 405


def test_pagination_walks_every_equity_point_once(market_client, alice, inst):
    _, h = alice
    rid = finished(market_client, h, body([inst.id]))["id"]
    seen, offset = [], 0
    while True:
        page = market_client.get(f"{API}/{rid}/instruments/{inst.id}/equity", headers=h, params={"limit": 100, "offset": offset}).json()
        seen += [p["bar_start"] for p in page["items"]]
        offset += 100
        if offset >= page["count"]:
            break
    assert len(seen) == len(set(seen)) == 375 and seen == sorted(seen)
    assert market_client.get(f"{API}/{rid}/instruments/{inst.id}/equity", headers=h, params={"limit": 5001}).status_code == 422


def test_run_list_is_newest_first_and_paginated(market_client, alice, inst):
    _, h = alice
    ids = [finished(market_client, h, body([inst.id]))["id"] for _ in range(3)]
    page = market_client.get(API, headers=h, params={"limit": 2}).json()
    assert page["count"] == 3 and [r["id"] for r in page["items"]] == ids[::-1][:2]


def test_the_strategy_registry(market_client, alice):
    data = market_client.get(f"{API}/strategies", headers=alice[1]).json()
    assert [s["strategy_id"] for s in data["strategies"]] == ["ORB", "VWAP", "EMA_CROSSOVER", "RSI_BOLLINGER", "SUPERTREND"]
    orb = data["strategies"][0]
    assert orb["defaults"]["entry_cutoff"] == "11:30" and orb["defaults"]["range_minutes"] == 15 and orb["family"] == "OPENING_BREAKOUT"
    assert data["defaults_status"] == "APPROVED_INITIAL_DEFAULTS"
    assert data["regime"] == {"adx_period": 14, "trend_min": 25.0, "range_max": 20.0, "checkpoint": "09:30"}
    assert data["engine"]["fill_model"] == "NEXT_BAR_OPEN_V1" and data["engine"]["default_cost_basis"] == "GROSS_OF_COSTS"


def test_a_backtest_never_writes_to_the_account_or_the_archive(market_client, alice, inst, db):
    info, h = alice
    account = db.scalar(select(VirtualAccount).where(VirtualAccount.user_id == uuid.UUID(info["user"]["id"])))
    before = (account.initial_capital, account.currency, db.scalar(select(func.count()).select_from(Candle)))
    finished(market_client, h, body([inst.id], capital="5000.00"))
    db.expire_all()
    account = db.get(VirtualAccount, account.id)
    assert (account.initial_capital, account.currency, db.scalar(select(func.count()).select_from(Candle))) == before


def test_creating_the_application_starts_no_backtest(md_settings, registry, clock, db, inst):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(md_settings, registry, clock)):
        pass
    assert counts(db)["backtest_runs"] == 0


def test_a_backtest_never_routes_anything_through_the_phase4_execution_path(market_client, alice, inst, db, md_settings):
    """Phase 3 fills are simulated by the backtest engine itself: a complete run (with Phase 4 execution disabled, its
    default) creates no order, fill, ledger entry, position, risk decision or risk state."""
    from app.db.models import CashLedgerEntry, Fill, Order, Position, RiskDecision, RiskState

    assert md_settings.execution_mode == "disabled"
    run = finished(market_client, alice[1], body([inst.id]))
    assert run["status"] == "SUCCEEDED" and run["fill_model"] == "NEXT_BAR_OPEN_V1"
    assert db.scalar(select(func.count()).select_from(BacktestTrade)) > 0
    for model in (Order, Fill, CashLedgerEntry, Position, RiskDecision, RiskState):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__tablename__
