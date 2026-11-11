"""Staging validation (B9): exercise the Phase 4 business flow against a COPY of the real Yahoo archive.

Safety:
  * runs only against a database whose name ends with `_staging` and differs from DB_NAME and DB_TEST_NAME;
  * never writes candles, corporate actions or any market data; it creates one validator user (and that user's
    orders, fills, ledger and risk records) inside the staging copy;
  * execution is enabled only inside this process, in `simulation_provider_native` mode, with the approval reference
    and cost schedule passed on the command line. Real-money execution does not exist.

Each check reports PASS, FAIL or NOT_EXERCISED (with the reason); nothing is reported as passed unless it ran.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.db.enums import (
    CorporateActionType,
    ExecutionPolicy,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    PriceBasis,
)
from app.db.models import (
    CashLedgerEntry,
    Candle,
    CorporateAction,
    DataProvider,
    DataQualityIssue,
    Fill,
    Instrument,
    Order,
    RiskSettings,
    User,
    VirtualAccount,
)
from app.db.session import make_engine, make_sessionmaker
from app.exchange.costs import FeeContext
from app.exchange.types import OrderIntent

D = Decimal


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    archive: dict = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"check": name, "status": status, "detail": detail})

    def expect(self, name: str, condition: bool, detail: str = "") -> bool:
        self.add(name, "PASS" if condition else "FAIL", detail)
        return condition

    @property
    def ok(self) -> bool:
        return all(c["status"] != "FAIL" for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "archive": self.archive, "checks": self.checks,
            "summary": {s: sum(1 for c in self.checks if c["status"] == s) for s in ("PASS", "FAIL", "NOT_EXERCISED")},
        }


def staging_settings(base: Settings, staging_db: str, schedule_path: str, approval_reference: str,
                     participation: str | None = None) -> Settings:
    if not staging_db.endswith("_staging") or staging_db in (base.db_name, base.db_test_name):
        raise SystemExit("refused: the staging database name must end with '_staging' and differ from DB_NAME/DB_TEST_NAME")
    return base.model_copy(update={
        "db_name": staging_db,
        "execution_mode": "simulation_provider_native",
        "execution_approval_reference": f"STAGING-ONLY: {approval_reference}",
        "execution_approved_providers": [base.marketdata_provider],
        "execution_approved_price_bases": [],
        "exchange_cost_schedule_path": schedule_path,
        "exchange_max_volume_participation": D(participation) if participation else None,
    })


def _scenario_bars(s, provider_id: int) -> tuple[Instrument, list[Candle]] | None:
    """The instrument/session with the longest run of contiguous, positive-volume canonical 5m bars."""
    best = None
    sessions = s.execute(
        select(Candle.instrument_id, Candle.session_date, func.count())
        .where(Candle.provider_id == provider_id, Candle.is_canonical.is_(True), Candle.volume > 0)
        .group_by(Candle.instrument_id, Candle.session_date)
        .order_by(func.count().desc(), Candle.session_date.desc())
        .limit(20)
    ).all()
    for inst_id, session_date, _ in sessions:
        bars = s.scalars(
            select(Candle).where(
                Candle.instrument_id == inst_id, Candle.provider_id == provider_id, Candle.session_date == session_date,
                Candle.is_canonical.is_(True), Candle.timeframe == "5m",
            ).order_by(Candle.bar_start_ts)
        ).all()
        run: list[Candle] = []
        for b in bars:
            if run and (b.bar_start_ts != run[-1].bar_end_ts or b.volume == 0):
                run = []
            if b.volume > 0:
                run.append(b)
            if len(run) >= 6:
                inst = s.get(Instrument, inst_id)
                if best is None or len(run) > len(best[1]):
                    best = (inst, list(run))
        if best and len(best[1]) >= 6:
            return best
    return best


def run_staging_validation(factory, settings: Settings) -> Report:
    from app.exchange.corporate_actions import CorporateActionService
    from app.exchange.portfolio import daily_pnl
    from app.orders.service import OrderService, TradingContext

    rep = Report()
    with factory() as s:
        provider = s.scalar(select(DataProvider).where(DataProvider.code == settings.marketdata_provider))
        if provider is None:
            rep.add("archive_present", "FAIL", f"provider {settings.marketdata_provider} not found in the staging copy")
            return rep
        total = s.scalar(select(func.count()).select_from(Candle).where(Candle.provider_id == provider.id))
        canonical = s.scalar(select(func.count()).select_from(Candle).where(
            Candle.provider_id == provider.id, Candle.is_canonical.is_(True)))
        bases = dict(s.execute(select(Candle.price_basis, func.count()).where(Candle.provider_id == provider.id)
                               .group_by(Candle.price_basis)).all())
        issues = dict(s.execute(select(DataQualityIssue.issue_type, func.count()).group_by(DataQualityIssue.issue_type)).all())
        cas = dict(s.execute(select(CorporateAction.action_type, func.count()).group_by(CorporateAction.action_type)).all())
        rep.archive = {
            "provider": provider.code, "candles": total, "canonical": canonical,
            "price_basis": {k.value: v for k, v in bases.items()},
            "data_quality_issues": {k.value: v for k, v in issues.items()},
            "corporate_actions": {k.value: v for k, v in cas.items()},
        }
        rep.expect("archive_present", total > 0, f"{total} candles")
        rep.expect("all_bars_provider_native",
                   set(bases) <= {PriceBasis.UNVERIFIED_PROVIDER_NATIVE},
                   "every archived bar is UNVERIFIED_PROVIDER_NATIVE (never relabelled)")
        rep.expect("archive_is_final_only",
                   s.scalar(select(func.count()).select_from(Candle).where(Candle.is_final.is_(False))) == 0)
        found = _scenario_bars(s, provider.id)
    if not found or len(found[1]) < 6:
        rep.add("scenario", "NOT_EXERCISED", "no session with 6 contiguous positive-volume bars")
        return rep
    inst, bars = found
    b = bars
    clock = Clock(b[0].bar_end_ts)
    before_candles = rep.archive["candles"]

    with factory() as s:
        user = User(email=f"staging-validator-{uuid.uuid4().hex[:8]}@example.invalid", password_hash="!")
        s.add(user)
        s.flush()
        account = VirtualAccount(user_id=user.id, currency="INR", initial_capital=D("1000000"))
        s.add(account)
        s.add(RiskSettings(user_id=user.id, max_risk_per_trade_pct=D("2"), max_position_allocation_pct=D("50"),
                           max_portfolio_exposure_pct=D("100"), max_daily_loss_pct=D("10"), max_drawdown_pct=D("20"),
                           max_position_quantity=None, resize_allowed=True))
        s.commit()
        uid = user.id

    def ctx(s):
        return TradingContext.build(s, settings, clock)

    def intent(**kw) -> OrderIntent:
        base = dict(client_order_id=f"stg-{uuid.uuid4().hex[:10]}", instrument_id=inst.id, side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=10, source=OrderSource.MANUAL,
                    stop_loss_price=(b[0].close * D("0.9")).quantize(D("0.01")))
        base.update(kw)
        return OrderIntent(**base)

    with factory() as s:
        c = ctx(s)
        rep.expect("execution_enabled_in_simulation_only", c.exchange.execution_policy is ExecutionPolicy.SIMULATION_PROVIDER_NATIVE,
                   f"policy={c.exchange.execution_policy}")
        buy_intent = intent()
        r = OrderService(c).submit(uid, buy_intent)
        s.commit()
        rep.expect("order_submission_pending", r.order.status is OrderStatus.PENDING,
                   r.order.reject_reason or "accepted; never filled at submission")
        dup = OrderService(ctx(s)).submit(uid, buy_intent)
        s.commit()
        rep.expect("duplicate_submission_is_idempotent", dup.replayed and dup.order.id == r.order.id)
        no_stop = OrderService(ctx(s)).submit(uid, intent(stop_loss_price=None))
        s.commit()
        rep.expect("risk_rejects_entry_without_stop", no_stop.order.status is OrderStatus.REJECTED,
                   no_stop.order.reject_reason or "")
        huge = OrderService(ctx(s)).submit(uid, intent(quantity=10_000_000))
        s.commit()
        rep.expect("risk_resizes_or_rejects_oversized_entry",
                   huge.order.status is OrderStatus.REJECTED or huge.order.quantity < 10_000_000,
                   f"{huge.decision.outcome.value if huge.decision else ''} {huge.order.quantity}")
        if huge.order.status is OrderStatus.PENDING:
            ctx(s).exchange.cancel(s.get(VirtualAccount, account.id), huge.order.id, "staging: not part of scenario")
            s.commit()
        order_id = r.order.id

    clock.now = b[1].bar_end_ts
    with factory() as s:
        c = ctx(s)
        c.exchange.process_open_orders(s.get(VirtualAccount, account.id))
        s.commit()
        fill = s.scalar(select(Fill).where(Fill.order_id == order_id))
        if not rep.expect("next_bar_open_fill", fill is not None, "market BUY filled on the next bar"):
            return rep
        costs = c.exchange.costs_for(s.get(Order, order_id).product, b[1].session_date)
        expected_price = costs.slipped_price(OrderSide.BUY, b[1].open)
        rep.expect("fill_price_is_next_bar_open_plus_slippage", fill.price == expected_price and fill.reference_price == b[1].open,
                   f"open={b[1].open} fill={fill.price} expected={expected_price}")
        rep.expect("fill_provenance", fill.price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE
                   and fill.execution_policy is ExecutionPolicy.SIMULATION_PROVIDER_NATIVE
                   and fill.price_source_ref == f"candle:{b[1].id}" and fill.price_as_of == b[1].bar_start_ts)
        recomputed = costs.fee(OrderSide.BUY, fill.turnover, FeeContext())
        rep.expect("fees_recomputed_independently", recomputed == fill.fee, f"fill={fill.fee} recomputed={recomputed}")
        ledger = sum(s.scalars(select(CashLedgerEntry.amount).where(CashLedgerEntry.user_id == uid)).all(), D(0))
        rep.expect("ledger_reconciles", ledger == -(fill.turnover + fill.fee), f"ledger={ledger}")
        cash = c.portfolio.cash_balance(s.get(VirtualAccount, account.id))
        rep.expect("cash_equals_initial_plus_ledger", cash == D("1000000") + ledger)
    with factory() as s:  # restart recovery: a second processing run (fresh session) books nothing new
        ctx(s).exchange.process_open_orders(s.get(VirtualAccount, account.id))
        s.commit()
        rep.expect("restart_recovery_books_nothing_twice",
                   s.scalar(select(func.count()).select_from(Fill).where(Fill.user_id == uid)) == 1)

    clock.now = b[2].bar_end_ts
    with factory() as s:
        sell = OrderService(ctx(s)).submit(uid, intent(side=OrderSide.SELL, stop_loss_price=None))
        s.commit()
        rep.expect("exit_accepted", sell.order.status is OrderStatus.PENDING, sell.order.reject_reason or "")
        sell_id = sell.order.id
    clock.now = b[3].bar_end_ts
    with factory() as s:
        ctx(s).exchange.process_open_orders(s.get(VirtualAccount, account.id))
        s.commit()
        sfill = s.scalar(select(Fill).where(Fill.order_id == sell_id))
        if rep.expect("exit_filled_next_bar", sfill is not None and sfill.reference_price == b[3].open):
            pnl = daily_pnl(s, uid, b[3].session_date)
            rep.expect("daily_pnl_matches_fills", pnl.realized_pnl == sfill.realized_pnl and pnl.fills == 2,
                       f"daily={pnl.realized_pnl} fill={sfill.realized_pnl}")

    clock.now = b[-1].bar_end_ts + timedelta(hours=6)  # long after the last bar: data is stale
    with factory() as s:
        stale = OrderService(ctx(s)).submit(uid, intent())
        s.commit()
        rep.expect("stale_data_rejected", stale.order.status is OrderStatus.REJECTED
                   and "STALE" in (stale.order.reject_reason or ""), stale.order.reject_reason or "")

    if settings.exchange_max_volume_participation is None:
        rep.add("partial_fills", "NOT_EXERCISED", "EXCHANGE_MAX_VOLUME_PARTICIPATION not set for this run")
    with factory() as s:
        split = s.scalar(select(CorporateAction).where(
            CorporateAction.instrument_id == inst.id,
            CorporateAction.action_type.in_((CorporateActionType.SPLIT, CorporateActionType.BONUS)),
            CorporateAction.ex_date > b[0].session_date))
        if split is None:
            rep.add("frozen_position_recovery", "NOT_EXERCISED",
                    "no split/bonus with an ex-date after the scenario session for this instrument in the archive")
        else:
            rep.add("frozen_position_recovery", "NOT_EXERCISED",
                    f"split {split.id} exists; recovery is covered by integration tests, not replayed on the copy")
        after = s.scalar(select(func.count()).select_from(Candle).where(Candle.provider_id == provider.id))
        rep.expect("archive_unmodified", after == before_candles, f"{after} candles")
    return rep


def staging_main(args) -> int:
    base = get_settings()
    settings = staging_settings(base, args.staging_db, str(args.schedule), args.approval_reference, args.participation)
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    report = run_staging_validation(make_sessionmaker(engine), settings)
    print(json.dumps(report.as_dict(), indent=2, default=str))
    return 0 if report.ok else 1
