"""Simulation-only execution on UNVERIFIED_PROVIDER_NATIVE data (D4 simulation decision, Yahoo Finance for the MVP).

All bars here are SYNTHETIC fixtures with the UNVERIFIED_PROVIDER_NATIVE label that real Yahoo data carries.
They prove the policy and safeguards, not the quality of Yahoo data.
"""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import (
    CorporateActionType,
    ExecutionPolicy,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceBasis,
)
from app.db.models import Candle, CorporateAction, DataQualityIssue, Fill
from app.marketdata.types import IssueType, Severity
from app.orders.service import InvalidOrder, TradingContext
from tests.integration.trading_support import (
    NOW,
    Clock,
    configure_risk,
    enabled_settings,
    intent,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    next_bar,
    process,
    simulation_settings,
    submit,
)

D = Decimal


@pytest.fixture
def sim(db, test_settings):
    provider = make_provider(db)
    inst = make_instrument(db)
    account = make_account(db)
    configure_risk(db, account)
    return dict(db=db, provider=provider, inst=inst, account=account, settings=simulation_settings(test_settings),
                verified=enabled_settings(test_settings), clock=Clock())


def ref(s, close="100", **kw):
    """Reference bar (decision bar), provider-native like Yahoo data."""
    return make_candle(s["db"], s["provider"], s["inst"], close, verified=False, **kw)


def nxt(s, open_, close=None, **kw):
    return next_bar(s["db"], s["provider"], s["inst"], s["clock"], open_, close, verified=False, **kw)


def go(s, order_intent, settings=None):
    return submit(s["db"], settings or s["settings"], s["clock"], s["account"], order_intent)


def run(s, settings=None):
    return process(s["db"], settings or s["settings"], s["clock"], s["account"])


def ctx(s, settings=None):
    return TradingContext.build(s["db"], settings or s["settings"], s["clock"])


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


# ---- the simulated fill and its full record ----------------------------------------------------------


def test_simulated_fill_records_signal_acceptance_candle_price_provider_and_basis(sim):
    db = sim["db"]
    decision_bar = ref(sim, "100")
    signal_at = NOW - timedelta(minutes=2)
    r = go(sim, intent(sim["inst"], qty=10, signal_at=signal_at))
    order = r.order
    assert order.status is OrderStatus.PENDING
    assert order.execution_policy is ExecutionPolicy.SIMULATION_PROVIDER_NATIVE
    assert order.signal_at == signal_at and order.accepted_at == NOW
    assert order.decision_price_ref == f"candle:{decision_bar.id}"

    fill_bar = nxt(sim, "101", "103")
    run(sim)
    fill = db.scalar(select(Fill))
    assert fill.execution_policy is ExecutionPolicy.SIMULATION_PROVIDER_NATIVE
    assert fill.price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE  # provenance retained, never relabelled
    assert fill.price_source_ref == f"candle:{fill_bar.id}" and fill.price_source_kind == "NEXT_BAR_OPEN"
    assert fill.reference_price == D("101") and fill.price == D("101.0505")  # next open + slippage
    assert fill.provider_code == "fixture"
    assert fill.price_as_of == fill_bar.bar_start_ts and fill.executed_at >= fill_bar.observed_at
    # the archived candle itself is untouched
    db.expire_all()
    assert db.get(Candle, fill_bar.id).price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE
    assert db.get(Candle, fill_bar.id).basis_verified_by is None


def test_signal_time_defaults_to_acceptance_and_cannot_be_in_the_future_or_naive(sim):
    ref(sim)
    assert go(sim, intent(sim["inst"])).order.signal_at == NOW
    with pytest.raises(InvalidOrder, match="future"):
        go(sim, intent(sim["inst"], signal_at=NOW + timedelta(seconds=1)))
    sim["db"].rollback()
    with pytest.raises(InvalidOrder, match="timezone-aware"):
        go(sim, intent(sim["inst"], signal_at=NOW.replace(tzinfo=None)))


# ---- modes never mix ---------------------------------------------------------------------------------


def test_verified_mode_never_fills_from_provider_native_data(sim):
    ref(sim)
    r = go(sim, intent(sim["inst"]), settings=sim["verified"])
    assert r.order.status is OrderStatus.REJECTED and "UNVERIFIED_BASIS" in r.order.reject_reason


def test_simulation_mode_does_not_accept_verified_labelled_data(sim):
    make_candle(sim["db"], sim["provider"], sim["inst"], "100", verified=True)
    r = go(sim, intent(sim["inst"]))
    assert r.order.status is OrderStatus.REJECTED and "BASIS_NOT_SIMULATION" in r.order.reject_reason


@pytest.mark.parametrize("switch_to", ["verified", "disabled"])
def test_an_order_is_only_filled_under_the_policy_it_was_accepted_with(sim, test_settings, switch_to):
    ref(sim)
    r = go(sim, intent(sim["inst"]))
    nxt(sim, "100")
    other = sim["verified"] if switch_to == "verified" else test_settings
    run(sim, settings=other)
    sim["db"].refresh(r.order)
    assert r.order.status is OrderStatus.CANCELLED and r.order.cancel_reason.startswith("EXECUTION_MODE_CHANGED")
    assert count(sim["db"], Fill) == 0


def test_database_keeps_simulation_and_verified_fills_apart(sim):
    db = sim["db"]
    ref(sim)
    go(sim, intent(sim["inst"], qty=10))
    nxt(sim, "100")
    run(sim)
    for sql in (
        "update fills set price_basis = 'AS_TRADED_VERIFIED'",  # (also blocked by append-only)
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, execution_policy, price_as_of,"
        " price_observed_at, cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity,"
        " reference_price, price, turnover, fee, price_source_kind, 'x', provider_code, 'AS_TRADED_VERIFIED',"
        " 'SIMULATION_PROVIDER_NATIVE', price_as_of, price_observed_at, cost_model, executed_at from fills limit 1",
        "insert into fills (user_id, order_id, instrument_id, seq, side, quantity, reference_price, price, turnover,"
        " fee, price_source_kind, price_source_ref, provider_code, price_basis, execution_policy, price_as_of,"
        " price_observed_at, cost_model, executed_at) select user_id, order_id, instrument_id, 9, side, quantity,"
        " reference_price, price, turnover, fee, price_source_kind, 'y', provider_code, 'UNVERIFIED_PROVIDER_NATIVE',"
        " 'VERIFIED_NEXT_BAR_OPEN', price_as_of, price_observed_at, cost_model, executed_at from fills limit 1",
        "update orders set execution_policy = 'VERIFIED_NEXT_BAR_OPEN'",
    ):
        with pytest.raises((IntegrityError, DBAPIError)):
            db.execute(text(sql))
            db.commit()
        db.rollback()


def test_signal_time_cannot_be_after_acceptance_in_the_database(sim):
    ref(sim, "102")
    r = go(sim, intent(sim["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    with pytest.raises((IntegrityError, DBAPIError)):
        sim["db"].execute(text("update orders set signal_at = accepted_at + interval '1 second' where id = :i"),
                          {"i": r.order.id})
        sim["db"].commit()
    sim["db"].rollback()


# ---- invalid, incomplete, missing, stale candles -------------------------------------------------------


def test_stale_reference_blocks_orders(sim):
    ref(sim, end=NOW - timedelta(minutes=16))
    assert "STALE" in go(sim, intent(sim["inst"])).order.reject_reason


def test_missing_next_bar_cancels_instead_of_filling_later(sim):
    ref(sim)
    r = go(sim, intent(sim["inst"]))
    sim["clock"].now = NOW + timedelta(minutes=5)
    nxt(sim, "100")  # the 05:00 bar is missing; this is 05:05
    run(sim)
    sim["db"].refresh(r.order)
    assert r.order.cancel_reason.startswith("DATA_GAP") and count(sim["db"], Fill) == 0


def test_a_limit_order_is_cancelled_on_a_gap_between_bars(sim):
    ref(sim, "102")
    r = go(sim, intent(sim["inst"], qty=5, otype=OrderType.LIMIT, limit_price=D("100")))
    nxt(sim, "101")  # 05:00, not marketable
    sim["clock"].now += timedelta(minutes=5)
    nxt(sim, "99")  # 05:10 would fill, but 05:05 is missing: the price path is unknown
    run(sim)
    sim["db"].refresh(r.order)
    assert r.order.cancel_reason.startswith("DATA_GAP") and count(sim["db"], Fill) == 0


def test_zero_volume_next_bar_is_not_a_fill(sim):
    ref(sim)
    r = go(sim, intent(sim["inst"]))
    nxt(sim, "100", volume=0)
    run(sim)
    sim["db"].refresh(r.order)
    assert r.order.cancel_reason.startswith("FILL_BAR_INELIGIBLE: ZERO_VOLUME")


@pytest.mark.parametrize("issue_type,severity,blocks", [
    (IssueType.RESTATEMENT, Severity.WARNING, True),
    (IssueType.DUPLICATE_CONFLICTING, Severity.ERROR, True),
    (IssueType.DUPLICATE_IDENTICAL, Severity.INFO, False),
])
def test_bars_with_recorded_quality_issues_are_rejected(sim, issue_type, severity, blocks):
    db = sim["db"]
    ref(sim)
    r = go(sim, intent(sim["inst"]))
    bar = nxt(sim, "100")
    db.add(DataQualityIssue(batch_id=bar.ingestion_batch_id, instrument_id=sim["inst"].id, issue_type=issue_type,
                            severity=severity, bar_start_ts=bar.bar_start_ts, message="fixture issue"))
    db.commit()
    run(sim)
    db.refresh(r.order)
    if blocks:
        assert r.order.cancel_reason.startswith("FILL_BAR_INELIGIBLE: DATA_QUALITY_ISSUE")
    else:
        assert r.order.status is OrderStatus.FILLED


def test_corporate_actions_unavailable_for_a_batch_blocks_its_bars(sim):
    db = sim["db"]
    c = ref(sim)
    db.add(DataQualityIssue(batch_id=c.ingestion_batch_id, instrument_id=sim["inst"].id,
                            issue_type=IssueType.CORPORATE_ACTIONS_UNAVAILABLE, severity=Severity.WARNING,
                            message="fixture: corporate actions fetch failed"))
    db.commit()
    assert "CORPORATE_ACTIONS_UNAVAILABLE" in go(sim, intent(sim["inst"])).order.reject_reason


# ---- corporate actions ------------------------------------------------------------------------------


def _split(s, ex: date):
    s["db"].add(CorporateAction(instrument_id=s["inst"].id, action_type=CorporateActionType.SPLIT, ex_date=ex,
                                ratio_numerator=D("2"), ratio_denominator=D("1"), source="fixture",
                                provider_id=s["provider"].id, observed_at=NOW))
    s["db"].commit()


def test_a_bar_captured_after_a_later_split_is_ambiguous_and_rejected(sim):
    # bar from 2026-01-05, but first observed on 2026-01-07, after a split with ex-date 2026-01-06: Yahoo may
    # already have adjusted it, so its basis is ambiguous.
    ref(sim, observed_delay=timedelta(days=2))
    _split(sim, date(2026, 1, 6))
    sim["clock"].now = NOW + timedelta(days=2)
    check = ctx(sim).gate.check(sim["inst"].id)
    assert check.rejection.value == "CORPORATE_ACTION_AFTER_BAR"


def test_a_bar_captured_before_the_split_is_not_affected(sim):
    ref(sim)
    _split(sim, date(2026, 1, 6))  # ex-date after the bar was observed: as captured, it predates the split
    assert ctx(sim).gate.check(sim["inst"].id).ok


def test_positions_held_across_a_split_are_frozen_until_adjusted(sim):
    db = sim["db"]
    ref(sim)
    go(sim, intent(sim["inst"], qty=10))
    nxt(sim, "100")
    run(sim)
    _split(sim, date(2026, 1, 6))
    sim["clock"].now = NOW.replace(day=6)  # next session, after the ex-date
    make_candle(db, sim["provider"], sim["inst"], "50", end=sim["clock"].now - timedelta(minutes=1), verified=False)
    v = ctx(sim).portfolio.valuation(sim["account"])
    assert v.equity is None
    assert v.positions[0].price_check.rejection.value == "CORPORATE_ACTION_UNADJUSTED_POSITION"
    r = go(sim, intent(sim["inst"], side=OrderSide.SELL, qty=10))
    assert r.order.status is OrderStatus.REJECTED and "CORPORATE_ACTION_UNADJUSTED_POSITION" in r.order.reject_reason


def test_dividends_do_not_block_execution(sim):
    db = sim["db"]
    ref(sim)
    db.add(CorporateAction(instrument_id=sim["inst"].id, action_type=CorporateActionType.DIVIDEND, ex_date=NOW.date(),
                           cash_amount=D("5"), source="fixture", provider_id=sim["provider"].id, observed_at=NOW))
    db.commit()
    assert go(sim, intent(sim["inst"])).order.status is OrderStatus.PENDING


# ---- configuration safeguards at runtime ---------------------------------------------------------------


@pytest.mark.parametrize("change,needle", [
    (dict(execution_approval_reference=None), "EXECUTION_APPROVAL_REFERENCE"),
    (dict(execution_approved_price_bases=[PriceBasis.AS_TRADED_VERIFIED]), "must be empty"),
    (dict(exchange_cost_schedule_path=None), "EXCHANGE_COST_SCHEDULE_PATH"),
])
def test_a_copied_settings_object_that_skips_validation_still_cannot_fill(sim, change, needle):
    # settings.model_copy() skips validators; the gate re-checks every safeguard at runtime
    ref(sim)
    r = go(sim, intent(sim["inst"]), settings=replace_settings(sim["settings"], **change))
    assert r.order.status is OrderStatus.REJECTED and "CONFIG_INVALID" in r.order.reject_reason
    assert needle in r.order.reject_reason


def replace_settings(settings, **change):
    return settings.model_copy(update=change)


def test_all_zero_costs_cannot_enable_simulation(sim, tmp_path):
    import json

    from tests.cost_fixtures import fixture_schedule_dict

    ref(sim)
    d = fixture_schedule_dict()
    for r in d["rates"]:
        r["value"] = "0"
        if r.get("cap") is not None:
            r["cap"] = "0"
    path = tmp_path / "zero.json"
    path.write_text(json.dumps(d))
    r = go(sim, intent(sim["inst"]), settings=replace_settings(sim["settings"], exchange_cost_schedule_path=str(path)))
    assert "is zero; zero costs cannot enable execution" in r.order.reject_reason


def test_simulation_status_and_disclosures_through_the_api(sim, test_settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import auth_headers, login, register_user

    app = create_app(simulation_settings(test_settings))
    app.state.clock = sim["clock"]
    with TestClient(app) as c:
        info = register_user(c)
        h = auth_headers(login(c, info["email"]))
        body = c.get("/api/v1/exchange/status", headers=h).json()
    assert body["execution_mode"] == "simulation_provider_native" and body["execution_enabled"] is True
    assert body["execution_policy"] == "SIMULATION_PROVIDER_NATIVE"
    assert body["execution_approval_reference"] == "TEST-FIXTURE-SIMULATION-APPROVAL"
    text_ = " ".join(body["disclosures"])
    assert "SIMULATION ONLY" in text_ and "UNVERIFIED_PROVIDER_NATIVE" in text_
    assert "Five-year intraday backtesting is NOT available" in text_
