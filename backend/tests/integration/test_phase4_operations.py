"""B5 dividends, B6 daily ingestion, B7 latency measurement, operator management and the B9 staging runner.
Market data here is SYNTHETIC (Phase 2's FakeProvider and fixture bars); no network, no real Yahoo data."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.enums import CorporateActionType, DividendCreditStatus, JobStatus, JobType, OrderSide, Timeframe
from app.db.models import (
    AuditEvent,
    Candle,
    CashLedgerEntry,
    CorporateAction,
    DataIngestionJob,
    DataProvider,
    DividendCredit,
    User,
)
from app.db.session import make_sessionmaker
from app.exchange.dividends import DividendError, DividendService, holding_at_ex_date_start
from app.exchange.portfolio import daily_pnl
from app.ops.cli import measure_latency, run_daily, set_operator
from tests.integration.test_phase4_final_fixes import (  # noqa: F401  (fixture `s` and helpers)
    JAN5,
    JAN6,
    adjust,
    at,
    bar,
    buy_and_fill,
    cas,
    ctx,
    go,
    run,
    s,
    split,
)
from tests.integration.trading_support import NOW, intent, make_account, make_candle
from tests.support import WEEKDAYS, day_bars, days_bars

D = Decimal
JAN7, JAN8 = date(2026, 1, 7), date(2026, 1, 8)


@pytest.fixture
def sf(engine):
    return make_sessionmaker(engine)


# =========================================== B6: daily ingestion ===========================================


def test_daily_ingests_reports_coverage_and_is_idempotent(sf, registry, md_settings, clock, provider):
    provider.bars = days_bars(WEEKDAYS)
    code, rep = run_daily(sf, registry, md_settings, clock=clock, coverage_days=14)
    assert code == 0 and rep["status"] == "SUCCEEDED"
    inserted = sum(r["candles_inserted"] for r in rep["instruments"])
    assert inserted > 0 and rep["seed"] is not None
    assert {c["symbol"] for c in rep["coverage"]["instruments"]} and "coverage_complete" in rep
    with sf() as db:
        before = db.scalar(select(func.count()).select_from(Candle))
    code2, rep2 = run_daily(sf, registry, md_settings, clock=clock, coverage_days=14)  # rerun: nothing new
    assert code2 == 0 and sum(r["candles_inserted"] for r in rep2["instruments"]) == 0
    with sf() as db:
        assert db.scalar(select(func.count()).select_from(Candle)) == before  # no duplicates


def test_daily_reports_incomplete_coverage_even_when_the_job_succeeds(sf, registry, md_settings, clock, provider):
    provider.bars = day_bars(WEEKDAYS[-1], first=0, last=40)  # only part of one session is available
    code, rep = run_daily(sf, registry, md_settings, clock=clock, coverage_days=7)
    assert rep["status"] in ("SUCCEEDED", "PARTIAL")
    assert rep["coverage_complete"] is False  # a successful API response is not proof of complete coverage
    assert any(c["missing_intervals"] > 0 or c["days_absent"] > 0 for c in rep["coverage"]["instruments"])


def test_daily_recovers_from_a_crashed_run(sf, registry, md_settings, clock, provider):
    provider.bars = days_bars(WEEKDAYS)
    with sf() as db:
        from app.marketdata.seed import seed_registry

        seed_registry(db, registry.descriptors())
        pid = db.scalar(select(DataProvider.id).where(DataProvider.code == "yahoo"))
        db.add(DataIngestionJob(job_type=JobType.INCREMENTAL, status=JobStatus.RUNNING, provider_id=pid,
                                timeframe=Timeframe.M5, heartbeat_at=clock() - timedelta(hours=2),
                                started_at=clock() - timedelta(hours=2)))
        db.commit()
    code, rep = run_daily(sf, registry, md_settings, clock=clock)
    assert code == 0
    with sf() as db:
        statuses = [j.status for j in db.scalars(select(DataIngestionJob).order_by(DataIngestionJob.created_at))]
    assert statuses[0] is JobStatus.INTERRUPTED and statuses[-1] is JobStatus.SUCCEEDED


def test_daily_refuses_to_overlap_a_live_run(sf, registry, md_settings, clock, provider):
    with sf() as db:
        from app.marketdata.seed import seed_registry

        seed_registry(db, registry.descriptors())
        pid = db.scalar(select(DataProvider.id).where(DataProvider.code == "yahoo"))
        db.add(DataIngestionJob(job_type=JobType.INCREMENTAL, status=JobStatus.RUNNING, provider_id=pid,
                                timeframe=Timeframe.M5, heartbeat_at=clock(), started_at=clock()))
        db.commit()
    code, rep = run_daily(sf, registry, md_settings, clock=clock)
    assert code == 3 and rep["status"] == "SKIPPED_ALREADY_RUNNING"


def test_daily_reports_provider_failures(sf, registry, md_settings, clock, provider):
    from app.marketdata.errors import ProviderUnavailable

    def down(*a, **k):
        raise ProviderUnavailable("provider down (fixture)")

    provider.fetch_history = down
    code, rep = run_daily(sf, registry, md_settings, clock=clock)
    assert code != 0 and rep["status"] in ("FAILED", "PARTIAL")


# =========================================== B7: latency ================================================


def test_latency_report_measures_availability_after_bar_end(s):
    for i, delay in enumerate([30, 60, 90, 300]):
        make_candle(s["db"], s["provider"], s["inst"], "100", end=NOW - timedelta(minutes=5 * (i + 1)),
                    verified=False, observed_delay=timedelta(seconds=delay))
    sf_ = make_sessionmaker(s["db"].get_bind())
    rep = measure_latency(sf_, "fixture", days=1, clock=lambda: NOW)
    assert rep["bars"] == 4
    assert rep["lag_seconds"]["min"] == 30 and rep["lag_seconds"]["max"] == 300 and rep["lag_seconds"]["p50"] in (60, 90)


# =========================================== operators ==================================================


def test_set_operator_is_server_side_and_audited(s):
    db = s["db"]
    email = db.get(User, s["account"].user_id).email
    sf_ = make_sessionmaker(db.get_bind())
    assert set_operator(sf_, email, True) == {"email": email, "is_operator": True, "changed": True}
    assert set_operator(sf_, email, True)["changed"] is False
    set_operator(sf_, email, False)
    db.expire_all()
    assert db.get(User, s["account"].user_id).is_operator is False
    events = db.scalars(select(AuditEvent.event_type).where(AuditEvent.event_type.like("operator.%"))).all()
    assert events.count("operator.granted") == 2 and events.count("operator.revoked") == 1


# =========================================== B5: dividends ===============================================


def operator_id(s):
    db = s["db"]
    op = make_account(db)
    db.execute(text("update users set is_operator = true where id = :u"), {"u": op.user_id})
    db.commit()
    return op.user_id


def dividend(s, ex=JAN7, amount="5"):
    a = CorporateAction(instrument_id=s["inst"].id, action_type=CorporateActionType.DIVIDEND, ex_date=ex,
                        cash_amount=D(amount), source="fixture dividend", provider_id=s["provider"].id, observed_at=NOW)
    s["db"].add(a)
    s["db"].commit()
    return a


def credit(s, action, op, amount="5", pay=JAN8, evidence="FIXTURE/record-date notice", accept=False):
    res = DividendService(s["db"], s["clock"]).credit(
        op, corporate_action_id=action.id, evidence_reference=evidence, confirmed_amount_per_share=D(amount),
        payment_date=pay, accept_provider_difference=accept,
    )
    s["db"].commit()
    return res


def cash(s, account=None):
    return ctx(s).portfolio.cash_balance(account or s["account"])


def test_entitlement_is_the_holding_at_the_close_before_the_ex_date(s):
    db = s["db"]
    buy_and_fill(s, qty=10)  # 5 Jan
    action = dividend(s)  # ex-date 7 Jan
    s["clock"].now = at(JAN7, 9, 20)
    make_candle(db, s["provider"], s["inst"], "100", end=s["clock"].now - timedelta(minutes=1), verified=False)
    go(s, intent(s["inst"], side=OrderSide.SELL, qty=4))  # sold ON the ex-date: still entitled
    bar(s, "100")
    run(s)
    assert holding_at_ex_date_start(db, s["account"].user_id, s["inst"].id, JAN7).quantity == 10
    assert holding_at_ex_date_start(db, s["account"].user_id, s["inst"].id, JAN5).quantity == 0  # bought on 5 Jan
    assert holding_at_ex_date_start(db, s["account"].user_id, s["inst"].id, date(2026, 1, 9)).quantity == 6


def test_operator_credit_books_a_separate_ledger_entry_once(s):
    db = s["db"]
    buy_and_fill(s, qty=10)
    action = dividend(s)
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    fills_before = [tuple(r) for r in db.execute(text("select id, price, fee from fills order by id"))]
    cash_before = cash(s)
    [res] = credit(s, action, op)
    c = res.credit
    assert c.status is DividendCreditStatus.APPLIED and not res.replayed
    assert (c.entitled_quantity, c.gross_amount, c.cum_date, c.payment_date) == (10, D("50"), date(2026, 1, 6), JAN8)
    assert c.operator_id == op and c.provider_amount_per_share == D("5")
    assert cash(s) == cash_before + D("50")
    entry = db.scalar(select(CashLedgerEntry).where(CashLedgerEntry.dividend_credit_id == c.id))
    assert entry.entry_type.value == "DIVIDEND" and entry.fill_id is None and entry.adjustment_id is None
    assert [tuple(r) for r in db.execute(text("select id, price, fee from fills order by id"))] == fills_before
    [again] = credit(s, action, op)  # restart / re-run: idempotent
    assert again.replayed and again.credit.id == c.id
    assert db.scalar(select(func.count()).select_from(CashLedgerEntry).where(CashLedgerEntry.entry_type == "DIVIDEND")) == 1
    pnl = daily_pnl(db, s["account"].user_id, JAN8)
    assert pnl.dividend_income == D("50") and pnl.dividends == 1 and pnl.realized_pnl == 0  # not trading P&L
    v = ctx(s).portfolio.valuation(s["account"])
    assert v.cash_balance == cash(s)  # equity includes the dividend through cash


@pytest.mark.parametrize("kwargs,reason", [
    (dict(pay=date(2026, 1, 20)), "PAYMENT_DATE_IN_FUTURE"),
    (dict(pay=date(2026, 1, 6)), "PAYMENT_DATE_INVALID"),
    (dict(amount="4.5"), "AMOUNT_DIFFERS_FROM_PROVIDER"),
    (dict(evidence="   "), "EVIDENCE_REQUIRED"),
])
def test_refusals_are_recorded_and_credit_nothing(s, kwargs, reason):
    db = s["db"]
    buy_and_fill(s, qty=10)
    action = dividend(s)
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    before = cash(s)
    [res] = credit(s, action, op, **kwargs)
    assert res.credit.status is DividendCreditStatus.REJECTED and res.credit.reason.startswith(reason)
    assert cash(s) == before
    assert db.scalar(select(func.count()).select_from(CashLedgerEntry).where(CashLedgerEntry.entry_type == "DIVIDEND")) == 0


def test_an_accepted_provider_difference_is_credited_with_both_values_recorded(s):
    buy_and_fill(s, qty=10)
    action = dividend(s, amount="10")  # e.g. provider value not adjusted for a later event
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    [res] = credit(s, action, op, amount="5", accept=True)
    assert res.credit.status is DividendCreditStatus.APPLIED and res.credit.gross_amount == D("50")
    assert res.credit.provider_amount_per_share == D("10") and res.credit.evidence["accept_provider_difference"] is True


def test_non_dividend_actions_and_bad_amounts_are_rejected_outright(s):
    buy_and_fill(s)
    sp = split(s, JAN6)
    op = operator_id(s)
    with pytest.raises(DividendError, match="not a dividend"):
        credit(s, sp, op)
    s["db"].rollback()
    with pytest.raises(DividendError, match="positive"):
        credit(s, dividend(s), op, amount="0")


def test_an_unadjusted_split_before_the_ex_date_blocks_the_credit_until_adjusted(s):
    db = s["db"]
    buy_and_fill(s, qty=10)  # 5 Jan
    sp = split(s, JAN6)  # 2:1 on 6 Jan
    action = dividend(s)  # ex 7 Jan
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    [blocked] = credit(s, action, op)
    assert blocked.credit.status is DividendCreditStatus.REJECTED
    assert blocked.credit.reason.startswith("UNADJUSTED_CORPORATE_ACTION")
    assert adjust(s, sp).adjustment.status.value == "APPLIED"
    [ok] = credit(s, action, op)
    assert ok.credit.status is DividendCreditStatus.APPLIED and ok.credit.entitled_quantity == 20  # post-split shares


def test_positions_closed_before_the_ex_date_are_not_entitled_and_users_are_credited_separately(s):
    db = s["db"]
    buy_and_fill(s, qty=10)
    go(s, intent(s["inst"], side=OrderSide.SELL, qty=10))
    bar(s, "100")
    run(s)  # closed on 5 Jan
    other = make_account(db)
    from tests.integration.trading_support import configure_risk

    configure_risk(db, other, max_position_allocation_pct=D("100"), max_risk_per_trade_pct=D("100"))
    s2 = dict(s, account=other)
    go(s2, intent(s["inst"], qty=7))
    bar(s, "100")
    run(s2)
    action = dividend(s)
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    results = credit(s, action, op)
    assert [(r.credit.user_id, r.credit.entitled_quantity) for r in results] == [(other.user_id, 7)]
    assert cash(s2, other) > 0


def test_dividend_records_are_append_only_and_applied_once_in_the_database(s):
    db = s["db"]
    buy_and_fill(s, qty=10)
    action = dividend(s)
    op = operator_id(s)
    s["clock"].now = at(JAN8, 12, 0)
    credit(s, action, op)
    for sql in (
        "update dividend_credits set gross_amount = 1",
        "delete from dividend_credits",
        "insert into dividend_credits (user_id, account_id, instrument_id, corporate_action_id, ex_date, cum_date,"
        " payment_date, entitled_quantity, confirmed_amount_per_share, gross_amount, evidence_reference, evidence,"
        " status, operator_id, credited_at) select user_id, account_id, instrument_id, corporate_action_id, ex_date,"
        " cum_date, payment_date, entitled_quantity, confirmed_amount_per_share, gross_amount, evidence_reference,"
        " evidence, 'APPLIED', operator_id, credited_at from dividend_credits",
        "insert into cash_ledger (user_id, account_id, fill_id, entry_type, amount) select user_id, account_id, id,"
        " 'DIVIDEND', 1 from fills limit 1",
    ):
        with pytest.raises((IntegrityError, DBAPIError)):
            db.execute(text(sql))
            db.commit()
        db.rollback()


def test_dividend_endpoints_are_operator_only_and_users_see_their_own(s, test_settings):
    from fastapi.testclient import TestClient

    from app.db.models import VirtualAccount
    from app.main import create_app
    from tests.conftest import auth_headers, login, register_user
    from tests.integration.trading_support import configure_risk, simulation_settings

    app = create_app(simulation_settings(test_settings))
    app.state.clock = s["clock"]
    db = s["db"]
    with TestClient(app) as c:
        info = register_user(c)
        h = auth_headers(login(c, info["email"]))
        acct = db.scalar(select(VirtualAccount).where(VirtualAccount.id == info["account"]["id"]))
        configure_risk(db, acct, max_position_allocation_pct=D("100"))
        buy_and_fill(dict(s, account=acct), qty=10)
        action = dividend(s)
        s["clock"].now = at(JAN8, 12, 0)
        body = {"evidence_reference": "FIXTURE/notice", "confirmed_amount_per_share": "5", "payment_date": "2026-01-08"}
        url = f"/api/v1/operator/dividends/{action.id}/credits"
        assert c.post(url, json=body, headers=h).status_code == 403
        assert c.get("/api/v1/operator/dividends/pending", headers=h).status_code == 403
        op_info = register_user(c)
        db.execute(text("update users set is_operator = true where email = :e"), {"e": op_info["email"]})
        db.commit()
        ho = auth_headers(login(c, op_info["email"]))
        pend = c.get("/api/v1/operator/dividends/pending", headers=ho).json()
        assert pend == [{"corporate_action_id": action.id, "instrument_id": s["inst"].id, "ex_date": "2026-01-07",
                         "provider_amount_per_share": "5.000000", "source": "fixture dividend",
                         "entitled_users": 1, "blocked_users": 0}]
        res = c.post(url, json=body, headers=ho).json()
        assert res[0]["credit"]["status"] == "APPLIED" and res[0]["credit"]["gross_amount"] == "50.000000"
        assert c.get("/api/v1/operator/dividends/pending", headers=ho).json() == []
        mine = c.get("/api/v1/portfolio/dividends", headers=h).json()
        assert len(mine) == 1 and mine[0]["user_id"] == info["user"]["id"]
        assert c.get("/api/v1/portfolio/dividends", headers=ho).json() == []
        bad = c.post(f"/api/v1/operator/dividends/{action.id + 999}/credits", json=body, headers=ho)
        assert bad.status_code == 422


# =========================================== B9: staging runner =========================================


def test_staging_runner_on_a_synthetic_archive(db, test_settings):
    """Exercises the runner end to end. The archive is SYNTHETIC: this proves the runner, not real Yahoo data."""
    from tests.cost_fixtures import FIXTURE_SCHEDULE_PATH
    from tests.integration.trading_support import make_instrument, make_provider

    provider = make_provider(db, "yahoo")
    inst = make_instrument(db, "STAGECO")
    start = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)  # 09:15 IST
    for i in range(10):
        make_candle(db, provider, inst, str(100 + i), end=start + timedelta(minutes=5 * (i + 1)), open=str(100 + i),
                    verified=False, observed_delay=timedelta(0))
    settings = test_settings.model_copy(update={
        "execution_mode": "simulation_provider_native", "execution_approval_reference": "STAGING-ONLY: test",
        "execution_approved_providers": ["yahoo"], "execution_approved_price_bases": [],
        "exchange_cost_schedule_path": str(FIXTURE_SCHEDULE_PATH),
    })
    from app.ops.staging import run_staging_validation

    rep = run_staging_validation(make_sessionmaker(db.get_bind()), settings).as_dict()
    failed = [c for c in rep["checks"] if c["status"] == "FAIL"]
    assert rep["ok"], failed
    passed = {c["check"] for c in rep["checks"] if c["status"] == "PASS"}
    assert {"next_bar_open_fill", "fill_price_is_next_bar_open_plus_slippage", "fees_recomputed_independently",
            "ledger_reconciles", "restart_recovery_books_nothing_twice", "duplicate_submission_is_idempotent",
            "daily_pnl_matches_fills", "stale_data_rejected", "archive_unmodified"} <= passed
    assert {c["check"] for c in rep["checks"] if c["status"] == "NOT_EXERCISED"} >= {"partial_fills"}


def test_staging_settings_refuse_non_staging_databases(test_settings):
    from app.ops.staging import staging_settings

    for name in ("agentx", test_settings.db_test_name, test_settings.db_name, "agentx_prod"):
        with pytest.raises(SystemExit, match="refused"):
            staging_settings(test_settings, name, "x.json", "ref")
    ok = staging_settings(test_settings, "agentx_staging", "x.json", "ref")
    assert ok.db_name == "agentx_staging" and ok.execution_mode == "simulation_provider_native"
    assert ok.execution_approval_reference.startswith("STAGING-ONLY")
