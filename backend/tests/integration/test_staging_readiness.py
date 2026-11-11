"""Staging readiness: realistic bar timing in the Mock Exchange (B9), the B7 freshness policy, immutable approval
references on orders and fills (D4/B1, migration 0010) and the SYNTHETIC_STAGING_ONLY schedule guard.

All bars are SYNTHETIC fixtures. Every bar is recorded *after* it closes (2-3 minutes, as live Yahoo ingestion
does; or much later, as a backfill does); none is recorded at the instant it closes."""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.enums import OrderSide, OrderStatus
from app.db.models import AuditEvent, Fill, Order, ProtectiveExitRequirement, RiskDecision
from app.db.session import make_sessionmaker
from app.exchange.costs import SYNTHETIC_STAGING_MARKER
from app.exchange.service import MockExchange
from app.ops.staging import staging_settings
from app.orders.service import OrderService, TradingContext
from tests.cost_fixtures import FIXTURE_SCHEDULE_PATH
from tests.integration.trading_support import (
    NOW,
    Clock,
    configure_risk,
    intent,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    process,
    simulation_settings,
    submit,
)

D = Decimal
LAG = timedelta(seconds=143)  # fastest recording lag seen in the real archive
FIVE = timedelta(minutes=5)
# NOW is 10:30 IST; bars on the 5-minute grid of that session.
B0_END = NOW - FIVE  # 10:25 IST


@pytest.fixture
def sim(db, test_settings):
    provider = make_provider(db)
    inst = make_instrument(db)
    account = make_account(db)
    configure_risk(db, account)
    return dict(db=db, provider=provider, inst=inst, account=account, settings=simulation_settings(test_settings))


def bar(s, end, open_="100", *, lag=LAG, close=None, volume=100_000):
    return make_candle(s["db"], s["provider"], s["inst"], close or open_, end=end, open=open_, verified=False,
                       observed_delay=lag, volume=volume)


def available(c):
    return max(c.bar_end_ts, c.observed_at)


# ---- realistic timing in the Mock Exchange ---------------------------------------------------------------------


def test_order_accepted_after_the_next_bar_started_fills_on_the_following_bar_once_recorded(sim):
    s = sim
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))  # the decision can only be taken once b0 is recorded (10:27:23 IST)
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    assert r.order.status is OrderStatus.PENDING and r.order.decision_price_ref == f"candle:{b0.id}"

    b1 = bar(s, B0_END + FIVE, "101")  # 10:25-10:30: started before acceptance -> can never fill the order
    b2 = bar(s, B0_END + 2 * FIVE, "102")  # 10:30-10:35: the first bar starting at or after acceptance

    clock.now = available(b1)  # b1 recorded, b2 not even started
    process(s["db"], s["settings"], clock, s["account"])
    clock.now = available(b2) - timedelta(seconds=1)  # b2 closed but not yet recorded: no lookahead
    process(s["db"], s["settings"], clock, s["account"])
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.PENDING
    assert s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id)) is None

    clock.now = available(b2)
    process(s["db"], s["settings"], clock, s["account"])
    fill = s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id))
    assert fill.price_source_ref == f"candle:{b2.id}" and fill.reference_price == D("102")
    assert fill.price_as_of == b2.bar_start_ts and fill.price_observed_at == b2.observed_at <= fill.executed_at
    assert fill.price_as_of >= r.order.accepted_at


def test_a_missing_fill_bar_cancels_rather_than_filling_on_a_later_bar(sim):
    s = sim
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    bar(s, B0_END + FIVE, "101")
    b3 = bar(s, B0_END + 3 * FIVE, "103")  # the 10:30 bar (the fill slot) is missing
    clock.now = available(b3)
    process(s["db"], s["settings"], clock, s["account"])
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("DATA_GAP")
    assert s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id)) is None


# ---- B7: freshness ---------------------------------------------------------------------------------------------


def test_late_ingestion_makes_the_reference_stale_and_the_rejection_is_recorded(sim):
    s = sim
    b0 = bar(s, B0_END, "100", lag=timedelta(minutes=16))  # recorded 16 min after its close
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "STALE" in r.order.reject_reason
    decision = s["db"].scalar(select(RiskDecision).where(RiskDecision.order_id == r.order.id))
    assert decision is not None and any("STALE" in reason for reason in decision.reasons)


def test_the_threshold_is_fifteen_minutes_by_default(test_settings):
    assert test_settings.execution_max_price_age_seconds == 900


# ---- protective exits when prices are stale (migration 0011) ----------------------------------------------------


def _long_position(s, stop="99"):
    """Buy 10 at the 10:30 bar's open; returns the clock positioned when that fill bar was recorded."""
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"], stop_loss_price=D(stop)))
    bar(s, B0_END + FIVE, "100")
    b2 = bar(s, B0_END + 2 * FIVE, "100")
    clock.now = available(b2)
    process(s["db"], s["settings"], clock, s["account"])
    assert s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id)) is not None
    return clock


def _protect(s, clock, settings=None):
    out = OrderService(TradingContext.build(s["db"], settings or s["settings"], clock)).run_protective_exits(s["account"].user_id)
    s["db"].commit()
    return out


def _requirements(s):
    return list(s["db"].scalars(select(ProtectiveExitRequirement).where(
        ProtectiveExitRequirement.user_id == s["account"].user_id).order_by(ProtectiveExitRequirement.id)))


def _alerts(s, event):
    return list(s["db"].scalars(select(AuditEvent).where(
        AuditEvent.user_id == s["account"].user_id, AuditEvent.event_type == event)))


def test_stale_price_records_an_unresolved_requirement_and_an_alert_without_any_exit(sim, caplog):
    s = sim
    clock = _long_position(s)
    # The last recorded bar traded through the stop, but by now it is 20 minutes old.
    stale = bar(s, B0_END + 3 * FIVE, "100", close="98", lag=LAG)
    clock.now = B0_END + 3 * FIVE + timedelta(minutes=20)
    with caplog.at_level(logging.ERROR, logger="agentx.orders"):
        exits = _protect(s, clock)
    assert exits == []  # no exit from stale data, no fabricated price
    assert s["db"].scalar(select(Order).where(Order.side == OrderSide.SELL)) is None
    [req] = _requirements(s)
    assert req.status == "OPEN" and req.reason_code == "STALE" and req.stale_trigger == "SL"
    assert req.quote_ref == f"candle:{stale.id}" and req.detected_at == clock.now
    assert len(_alerts(s, "risk.protective_exit_unresolved")) == 1
    assert any("protective exit unresolved" in rec.getMessage() for rec in caplog.records)


def test_repeated_stale_runs_keep_one_requirement_and_one_alert(sim):
    s = sim
    clock = _long_position(s)
    clock.now += timedelta(minutes=20)
    for _ in range(3):
        _protect(s, clock)
        clock.now += timedelta(minutes=1)
    [req] = _requirements(s)
    assert req.status == "OPEN" and req.checks == 3 and req.stale_trigger is None
    assert len(_alerts(s, "risk.protective_exit_unresolved")) == 1


def test_a_crossing_seen_on_the_stale_bar_exits_once_fresh_data_returns(sim):
    s = sim
    clock = _long_position(s)
    bar(s, B0_END + 3 * FIVE, "100", close="98", lag=LAG)  # crosses the 99 stop
    clock.now = B0_END + 3 * FIVE + timedelta(minutes=20)
    _protect(s, clock)
    # Fresh data returns above the stop: the observed crossing is still owed, so the exit is submitted.
    fresh = bar(s, B0_END + 8 * FIVE, "100.5", close="100.5", lag=LAG)
    clock.now = available(fresh)
    [result] = _protect(s, clock)
    assert result.order.side is OrderSide.SELL and result.order.status is OrderStatus.PENDING
    [req] = _requirements(s)
    assert req.status == "RESOLVED" and req.resolution == "EXIT_SUBMITTED" and req.exit_order_id == result.order.id
    assert len(_alerts(s, "risk.protective_exit_resolved")) == 1
    # Running again cannot create a second exit (the position's quantity is already committed to it).
    assert _protect(s, clock) == []
    assert len(list(s["db"].scalars(select(Order).where(Order.side == OrderSide.SELL)))) == 1


def test_no_crossing_resolves_as_not_triggered_on_fresh_data(sim):
    s = sim
    clock = _long_position(s)
    clock.now += timedelta(minutes=20)
    _protect(s, clock)
    fresh = bar(s, B0_END + 8 * FIVE, "100.2", lag=LAG)
    clock.now = available(fresh)
    assert _protect(s, clock) == []
    [req] = _requirements(s)
    assert req.status == "RESOLVED" and req.resolution == "NOT_TRIGGERED" and req.exit_order_id is None


def test_recovery_exit_fills_once_and_the_position_closes(sim):
    s = sim
    clock = _long_position(s)
    bar(s, B0_END + 3 * FIVE, "100", close="98", lag=LAG)
    clock.now = B0_END + 3 * FIVE + timedelta(minutes=20)
    _protect(s, clock)
    fresh = bar(s, B0_END + 8 * FIVE, "100.5", lag=LAG)
    clock.now = available(fresh)
    [result] = _protect(s, clock)
    nxt = bar(s, B0_END + 10 * FIVE, "100.4", lag=LAG)  # first slot after the exit was accepted
    clock.now = available(nxt)
    process(s["db"], s["settings"], clock, s["account"])
    process(s["db"], s["settings"], clock, s["account"])  # a restart books nothing twice
    fills = list(s["db"].scalars(select(Fill).where(Fill.order_id == result.order.id)))
    assert len(fills) == 1 and fills[0].reference_price == D("100.4")
    assert fills[0].execution_approval_reference == s["settings"].execution_approval_reference


def test_a_requirement_is_resolved_when_the_position_is_closed_by_other_means(sim):
    s = sim
    clock = _long_position(s)
    clock.now += timedelta(minutes=20)
    _protect(s, clock)
    fresh = bar(s, B0_END + 8 * FIVE, "100", lag=LAG)
    clock.now = available(fresh)
    sell = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"], side=OrderSide.SELL, stop_loss_price=None))
    assert sell.order.status is OrderStatus.PENDING
    nxt = bar(s, B0_END + 10 * FIVE, "100", lag=LAG)  # first slot after the sell was accepted
    clock.now = available(nxt)
    process(s["db"], s["settings"], clock, s["account"])
    _protect(s, clock)
    [req] = _requirements(s)
    assert req.resolution == "POSITION_CLOSED"


def test_requirements_are_append_only_and_resolved_ones_are_frozen(sim, engine):
    s = sim
    clock = _long_position(s)
    bar(s, B0_END + 3 * FIVE, "100", close="98", lag=LAG)
    clock.now = B0_END + 3 * FIVE + timedelta(minutes=20)
    _protect(s, clock)
    [req] = _requirements(s)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="cannot be deleted"):
        conn.execute(text("DELETE FROM protective_exit_requirements WHERE id = :i"), {"i": req.id})
    with engine.begin() as conn, pytest.raises(DBAPIError, match="stale trigger cannot be cleared"):
        conn.execute(text("UPDATE protective_exit_requirements SET stale_trigger = NULL WHERE id = :i"), {"i": req.id})
    with engine.begin() as conn, pytest.raises(DBAPIError):  # one OPEN requirement per position
        conn.execute(text(
            "INSERT INTO protective_exit_requirements (user_id, account_id, position_id, instrument_id, status, "
            "reason_code, detail, detected_at, last_checked_at) SELECT user_id, account_id, position_id, instrument_id, "
            "'OPEN', 'STALE', 'dup', detected_at, last_checked_at FROM protective_exit_requirements WHERE id = :i"),
            {"i": req.id})
    fresh = bar(s, B0_END + 8 * FIVE, "100.5", lag=LAG)
    clock.now = available(fresh)
    _protect(s, clock)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="resolved"):
        conn.execute(text("UPDATE protective_exit_requirements SET checks = checks + 1 WHERE id = :i"), {"i": req.id})


def test_protective_exit_api_alerts_operators_and_is_isolated(db, test_settings):
    from app.ops.cli import set_operator
    from tests.integration.test_trading_api import _client, _user, order_body

    settings = simulation_settings(test_settings)
    provider, inst = make_provider(db), make_instrument(db)
    with _client(settings) as c:
        clock = c.app.state.clock
        b0 = make_candle(db, provider, inst, "100", end=B0_END, verified=False, observed_delay=LAG)
        clock.now = available(b0)
        h, other = _user(c), _user(c)
        assert c.post("/api/v1/orders", json=order_body(inst.id, stop_loss_price="99"), headers=h).status_code == 201
        make_candle(db, provider, inst, "100", end=B0_END + FIVE, verified=False, observed_delay=LAG)
        b2 = make_candle(db, provider, inst, "100", end=B0_END + 2 * FIVE, verified=False, observed_delay=LAG)
        clock.now = available(b2)
        c.post("/api/v1/exchange/process", headers=h)
        clock.now += timedelta(minutes=20)  # stale
        run = c.post("/api/v1/exchange/protective-exits", headers=h).json()
        assert run["submitted_exits"] == [] and len(run["unresolved"]) == 1
        assert run["risk"]["unresolved_protective_exits"] == 1
        assert c.get("/api/v1/portfolio", headers=h).json()["positions"][0]["protective_exit_unresolved"] is True
        assert len(c.get("/api/v1/risk/protective-exits?status=OPEN", headers=h).json()) == 1
        assert c.get("/api/v1/risk/protective-exits", headers=other).json() == []
        assert c.get("/api/v1/operator/protective-exits/unresolved", headers=h).status_code == 403
        me = c.get("/api/v1/auth/me", headers=h).json()["email"]
        set_operator(make_sessionmaker(db.get_bind()), me, True)
        alerts = c.get("/api/v1/operator/protective-exits/unresolved", headers=h).json()
        assert [a["reason_code"] for a in alerts] == ["STALE"]


def test_downgrading_0011_is_refused_while_requirements_exist(sim, engine):
    from tests.integration.test_exchange import _downgrade, _head, _version

    clock = _long_position(sim)
    clock.now += timedelta(minutes=20)
    _protect(sim, clock)
    sim["db"].rollback()
    with pytest.raises(RuntimeError, match="cannot downgrade 0011"):
        _downgrade(engine, "0010")
    assert _version(engine) == _head()


# ---- D4/B1: approval reference on orders and fills (migration 0010) -------------------------------------------


def _filled(s):
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    bar(s, B0_END + FIVE, "101")
    b2 = bar(s, B0_END + 2 * FIVE, "102")
    clock.now = available(b2)
    process(s["db"], s["settings"], clock, s["account"])
    fill = s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id))
    return r.order, fill, clock


def test_orders_and_fills_carry_the_approval_in_force_at_acceptance(sim):
    order, fill, _ = _filled(sim)
    ref = sim["settings"].execution_approval_reference
    assert order.execution_approval_reference == ref
    assert fill.execution_approval_reference == ref
    assert fill.price_basis.value == "UNVERIFIED_PROVIDER_NATIVE" and fill.execution_policy.value == "SIMULATION_PROVIDER_NATIVE"


def test_orders_accepted_while_execution_is_disabled_have_no_approval_reference(db, test_settings):
    provider, inst, account = make_provider(db), make_instrument(db), make_account(db)
    configure_risk(db, account)
    make_candle(db, provider, inst, "100", verified=False)
    r = submit(db, test_settings, Clock(), account, intent(inst))
    assert r.order.status is OrderStatus.REJECTED
    assert r.order.execution_policy is None and r.order.execution_approval_reference is None


def test_the_database_refuses_to_change_an_orders_approval_reference(sim, engine):
    s = sim
    b0 = bar(s, B0_END, "100")
    r = submit(s["db"], s["settings"], Clock(available(b0)), s["account"], intent(s["inst"]))
    assert r.order.status is OrderStatus.PENDING
    with engine.begin() as conn, pytest.raises(DBAPIError, match="immutable"):
        conn.execute(update(Order).where(Order.id == r.order.id).values(execution_approval_reference="OTHER"))


def test_the_database_refuses_a_fill_whose_reference_differs_from_its_order(sim, engine):
    _, fill, _ = _filled(sim)
    cols = {c.key: getattr(fill, c.key) for c in Fill.__table__.columns if c.key not in ("id", "seq", "price_source_ref")}
    cols.update(seq=2, price_source_ref="candle:forged", execution_approval_reference="FORGED-APPROVAL")
    with engine.begin() as conn, pytest.raises(DBAPIError, match="approval reference"):
        conn.execute(Fill.__table__.insert().values(**cols))
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("UPDATE fills SET execution_approval_reference = 'X' WHERE id = :i"), {"i": fill.id})


def test_an_order_is_never_filled_under_a_different_approval(sim):
    s = sim
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    bar(s, B0_END + FIVE, "101")
    b2 = bar(s, B0_END + 2 * FIVE, "102")
    clock.now = available(b2)
    other = s["settings"].model_copy(update={"execution_approval_reference": "A-DIFFERENT-APPROVAL"})
    process(s["db"], other, clock, s["account"])
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("EXECUTION_APPROVAL_CHANGED")
    assert s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id)) is None


def test_a_cancelled_order_never_fills_even_after_the_approval_is_restored(sim):
    s = sim
    b0 = bar(s, B0_END, "100")
    clock = Clock(available(b0))
    r = submit(s["db"], s["settings"], clock, s["account"], intent(s["inst"]))
    bar(s, B0_END + FIVE, "101")
    b2 = bar(s, B0_END + 2 * FIVE, "102")
    clock.now = available(b2) - timedelta(seconds=1)  # fill bar not yet recorded
    other = s["settings"].model_copy(update={"execution_approval_reference": "A-DIFFERENT-APPROVAL"})
    process(s["db"], other, clock, s["account"])
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.reserved_cash == 0
    clock.now = available(b2)
    process(s["db"], s["settings"], clock, s["account"])  # original approval back, fill bar now recorded
    s["db"].refresh(r.order)
    assert r.order.status is OrderStatus.CANCELLED
    assert s["db"].scalar(select(Fill).where(Fill.order_id == r.order.id)) is None


def test_historical_null_references_stay_valid(sim, engine):
    """Rows from before 0010 keep NULL: the NOT VALID checks bind new rows only, and validating them is not required."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
            "('ck_orders_policy_has_approval_reference', 'ck_fills_has_approval_reference') ORDER BY conname")).all()
    assert rows == [("ck_fills_has_approval_reference", False), ("ck_orders_policy_has_approval_reference", False)]


def test_an_execution_policy_cannot_be_built_without_its_approval_reference(sim):
    from app.db.enums import ExecutionPolicy

    with pytest.raises(ValueError, match="approval reference"):
        MockExchange(sim["db"], None, None, Clock(), execution_policy=ExecutionPolicy.SIMULATION_PROVIDER_NATIVE)


def test_approval_references_are_served_by_the_api_and_isolated(db, test_settings):
    from tests.integration.test_trading_api import _client, _user, order_body

    settings = simulation_settings(test_settings)
    provider, inst = make_provider(db), make_instrument(db)
    with _client(settings) as c:
        clock = c.app.state.clock
        b0 = make_candle(db, provider, inst, "100", end=B0_END, verified=False, observed_delay=LAG)
        clock.now = available(b0)
        h, other = _user(c), _user(c)
        order = c.post("/api/v1/orders", json=order_body(inst.id), headers=h).json()["order"]
        assert order["execution_approval_reference"] == settings.execution_approval_reference
        make_candle(db, provider, inst, "101", end=B0_END + FIVE, verified=False, observed_delay=LAG)
        b2 = make_candle(db, provider, inst, "102", end=B0_END + 2 * FIVE, verified=False, observed_delay=LAG)
        clock.now = available(b2)
        assert c.post("/api/v1/exchange/process", headers=h).status_code == 200
        fills = c.get("/api/v1/portfolio/fills", headers=h).json()
        assert [f["execution_approval_reference"] for f in fills] == [settings.execution_approval_reference]
        assert c.get("/api/v1/portfolio/fills", headers=other).json() == []
        assert c.get(f"/api/v1/orders/{order['id']}", headers=other).status_code == 404


def test_downgrading_0010_is_refused_while_references_exist(sim, engine):
    from tests.integration.test_exchange import _downgrade, _head, _version

    _filled(sim)
    sim["db"].rollback()  # end the test session's read transaction: DDL in the downgrade needs exclusive locks
    with pytest.raises(RuntimeError, match="cannot downgrade 0010"):
        _downgrade(engine, "0009")
    assert _version(engine) == _head()


# ---- SYNTHETIC_STAGING_ONLY schedule guard ---------------------------------------------------------------------


@pytest.fixture
def synthetic_schedule(tmp_path):
    doc = json.loads(FIXTURE_SCHEDULE_PATH.read_text(encoding="utf-8"))
    doc["schedule_version"] = f"{SYNTHETIC_STAGING_MARKER}-test.1"
    path = tmp_path / "staging_schedule.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_synthetic_staging_schedule_is_refused_outside_the_staging_validator(test_settings, synthetic_schedule):
    dev_like = simulation_settings(test_settings, exchange_cost_schedule_path=str(synthetic_schedule))
    problems = dev_like.execution_config_problems(NOW.date())
    assert any(SYNTHETIC_STAGING_MARKER in p for p in problems)
    # Renaming the database alone is not enough: the approval must be the validator's STAGING-ONLY reference.
    renamed = dev_like.model_copy(update={"db_name": "agentx_staging"})
    assert any(SYNTHETIC_STAGING_MARKER in p for p in renamed.execution_config_problems(NOW.date()))


def test_a_synthetic_staging_schedule_is_accepted_by_the_staging_validator(test_settings, synthetic_schedule):
    staging = staging_settings(test_settings, "agentx_staging", str(synthetic_schedule), "owner-ref")
    assert staging.is_staging_run
    assert staging.execution_approval_reference == "STAGING-ONLY: owner-ref"
    assert staging.execution_config_problems(NOW.date()) == []


def test_an_already_labelled_staging_reference_is_kept_verbatim(test_settings, synthetic_schedule):
    ref = "STAGING-ONLY:AGENTX-MOCK-EXCHANGE-VALIDATION-001"
    staging = staging_settings(test_settings, "agentx_staging", str(synthetic_schedule), ref)
    assert staging.execution_approval_reference == ref and staging.is_staging_run


def test_environment_variables_cannot_unlock_the_synthetic_schedule(monkeypatch, synthetic_schedule):
    """Every configurable ingredient set (staging database name, STAGING-ONLY reference, simulation mode, even an
    env var named like the private flag): still refused, because only the validator process can mark itself."""
    from app.core.config import Settings

    for k, v in {
        "DB_NAME": "agentx_staging", "EXECUTION_MODE": "disabled", "EXECUTION_APPROVAL_REFERENCE": "STAGING-ONLY:x",
        "_STAGING_VALIDATOR_PROCESS": "true", "STAGING_VALIDATOR_PROCESS": "true",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings(_staging_validator_process=True)
    assert not s.is_staging_run
    enabled = s.model_copy(update={
        "execution_mode": "simulation_provider_native", "execution_approved_providers": ["yahoo"],
        "exchange_cost_schedule_path": str(synthetic_schedule),
    })
    assert any(SYNTHETIC_STAGING_MARKER in p for p in enabled.execution_config_problems(NOW.date()))
    # Settings validation at start-up refuses it as well (an enabled mode is checked on construction).
    monkeypatch.setenv("EXECUTION_MODE", "simulation_provider_native")
    monkeypatch.setenv("EXECUTION_APPROVED_PROVIDERS", '["yahoo"]')
    monkeypatch.setenv("EXCHANGE_COST_SCHEDULE_PATH", str(synthetic_schedule))
    with pytest.raises(ValueError, match=SYNTHETIC_STAGING_MARKER):
        Settings()


def test_the_staging_schedule_file_is_synthetic_and_valid_when_present():
    """The real staging file lives outside Git (data/ is ignored); when present it must be labelled and valid."""
    from datetime import date
    from pathlib import Path

    from app.db.enums import Product
    from app.exchange.costs import load_cost_schedule

    path = Path(__file__).resolve().parents[3] / "data" / "staging" / "cost_schedule.SYNTHETIC_STAGING_ONLY.json"
    if not path.exists():
        pytest.skip("staging schedule not present on this machine (it is never committed)")
    sched = load_cost_schedule(path)
    assert sched.is_synthetic_staging_only
    assert sched.problems(Product.DELIVERY, date(2026, 11, 12)) == []
    assert all("SYNTHETIC_STAGING_ONLY" in r.source for r in sched.file.rates)


# ---- scripted API lifecycle (app.ops.staging_lifecycle) ---------------------------------------------------------


def _lifecycle_settings(test_settings, tmp_path):
    doc = json.loads(FIXTURE_SCHEDULE_PATH.read_text(encoding="utf-8"))
    for r in doc["rounding"]:
        r["stage"] = "FILL"  # per-fill rounding, as in the staging schedule, so fees recompute fill by fill
    path = tmp_path / "lifecycle_schedule.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return test_settings.model_copy(update={
        "execution_mode": "simulation_provider_native", "execution_approval_reference": "STAGING-ONLY:LIFECYCLE-TEST",
        "execution_approved_providers": ["yahoo"], "execution_approved_price_bases": [],
        "exchange_cost_schedule_path": str(path),
    })


def _archive_sessions(db, sessions):
    provider = make_provider(db, "yahoo")
    inst = make_instrument(db, "LIFECO")
    for day_open, n in sessions:
        for i in range(n):
            lag = timedelta(seconds=140 + (i * 7) % 40)  # 2-3 minutes, never at the close
            make_candle(db, provider, inst, str(100 + (i % 3)), end=day_open + timedelta(minutes=5 * (i + 1)),
                        open=str(100 + (i % 3)), verified=False, observed_delay=lag)
    return inst


def test_lifecycle_over_two_sessions_exercises_stale_detection_and_recovery(db, test_settings, tmp_path):
    from datetime import UTC, datetime

    from app.ops.staging_lifecycle import run_lifecycle

    day1 = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
    _archive_sessions(db, [(day1, 20), (day1 + timedelta(days=1), 12)])
    rep = run_lifecycle(_lifecycle_settings(test_settings, tmp_path), make_sessionmaker(db.get_bind())).as_dict()
    failed = [c for c in rep["checks"] if c["status"] == "FAIL"]
    assert rep["ok"] and rep["order_flow_exercised"], failed
    passed = {c["check"] for c in rep["checks"] if c["status"] == "PASS"}
    assert {"exchange_status_enabled_simulation_only", "stop_loss_required", "oversized_entry_resized_or_rejected",
            "valid_delivery_buy_accepted", "order_approval_reference", "cash_reserved", "idempotent_replay",
            "reused_client_order_id_conflict", "entry_filled_on_expected_bar", "fill_price_next_eligible_open_plus_slippage",
            "fees_from_synthetic_schedule", "reservation_released_after_fill", "cash_reconciles", "ledger_reconciles_after_entry",
            "kill_switch_blocks_entries", "stale_price_blocks_new_orders", "protective_exit_unresolved_on_stale_data",
            "position_reported_unprotected", "protective_exit_alert_recorded", "protective_exit_recovers_on_fresh_data",
            "exit_filled_on_expected_bar", "exit_fees_from_synthetic_schedule", "realized_pnl_reconciles",
            "final_cash_reconciles", "ledger_reconciles_after_exit", "no_duplicate_fills"} <= passed
    assert rep["plan"]["stale_at"] is not None


def test_lifecycle_on_one_session_reports_recovery_not_exercised(db, test_settings, tmp_path):
    from datetime import UTC, datetime

    from app.ops.staging_lifecycle import run_lifecycle

    _archive_sessions(db, [(datetime(2026, 1, 5, 3, 45, tzinfo=UTC), 12)])
    rep = run_lifecycle(_lifecycle_settings(test_settings, tmp_path), make_sessionmaker(db.get_bind())).as_dict()
    assert rep["ok"] and rep["order_flow_exercised"], [c for c in rep["checks"] if c["status"] == "FAIL"]
    statuses = {c["check"]: c["status"] for c in rep["checks"]}
    assert statuses["protective_exit_stale_and_recovery"] == "NOT_EXERCISED"
    assert statuses["stale_price_blocks_new_orders"] == "PASS"


def test_lifecycle_on_a_backfilled_archive_exercises_nothing(db, test_settings, tmp_path):
    from datetime import UTC, datetime

    from app.ops.staging_lifecycle import run_lifecycle

    provider = make_provider(db, "yahoo")
    inst = make_instrument(db, "LIFECO")
    start = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
    for i in range(15):
        make_candle(db, provider, inst, "100", end=start + timedelta(minutes=5 * (i + 1)), verified=False,
                    observed_delay=timedelta(days=2))
    rep = run_lifecycle(_lifecycle_settings(test_settings, tmp_path), make_sessionmaker(db.get_bind()))
    assert rep.ok and not rep.exercised


def test_replay_clock_advances_with_real_time_from_its_start():
    from datetime import UTC, datetime

    from app.ops.staging import ReplayClock

    real = [datetime(2026, 11, 12, 18, 0, tzinfo=UTC)]
    clock = ReplayClock(datetime(2026, 11, 12, 4, 0, tzinfo=UTC), real_now=lambda: real[0])
    assert clock() == datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
    real[0] += timedelta(minutes=7)
    assert clock() == datetime(2026, 11, 12, 4, 7, tzinfo=UTC)


def test_staging_tools_refuse_non_staging_databases(test_settings):
    import argparse

    from app.ops.staging import staging_serve_main
    from app.ops.staging_lifecycle import lifecycle_main

    for db_name in ("agentx", test_settings.db_test_name, "agentx_prod"):
        args = argparse.Namespace(staging_db=db_name, schedule="x.json", approval_reference="r", participation=None,
                                  port=8020, replay_from=None)
        with pytest.raises(SystemExit, match="refused"):
            staging_serve_main(args)
        with pytest.raises(SystemExit, match="refused"):
            lifecycle_main(args)
