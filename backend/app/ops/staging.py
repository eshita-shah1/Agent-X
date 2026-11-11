"""Staging validation (B9): exercise the Phase 4 business flow against a COPY of the real Yahoo archive.

Safety:
  * runs only against a database whose name ends with `_staging` and differs from DB_NAME and DB_TEST_NAME;
  * never writes candles, corporate actions or any market data; it creates one validator user (and that user's
    orders, fills, ledger and risk records) inside the staging copy;
  * execution is enabled only inside this process, in `simulation_provider_native` mode, with the approval reference
    and cost schedule passed on the command line. Real-money execution does not exist.

Time model (no lookahead): a bar can be used only once it has ended AND Agent-X has recorded it, i.e. from
`max(bar_end_ts, observed_at)` (`ArchivedBar.available_at`). Real bars are always recorded after they close (the
finalisation margin alone is 60 s; backfilled bars are recorded days later), so the simulated clock is never set to a
bar's close when its recording is later. The scenario is planned from the archive with the same rules the price gate
and the Mock Exchange apply:
  * the reference price at a decision time is the latest bar already ended and recorded, and it must be fresh
    (age <= EXECUTION_MAX_PRICE_AGE_SECONDS);
  * a MARKET order accepted at time t fills on the bar that starts at the first bar boundary at or after t, in the
    same session; that bar is not necessarily the chronologically next one after the decision bar, and if it is
    missing or ineligible no fill is expected;
  * processing happens only once that fill bar has been recorded.
If the archive has no such scenario (e.g. it was only backfilled), the order flow is reported NOT_EXERCISED, never
passed and never fabricated.

Each check reports PASS, FAIL or NOT_EXERCISED (with the reason); nothing is reported as passed unless it ran.
"""

from __future__ import annotations

import bisect
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
from app.exchange.pricing import session_date_of
from app.exchange.types import OrderIntent
from app.marketdata.calendar import timeframe_minutes
from app.marketdata.types import IssueType, Severity

D = Decimal
STAGING_REFERENCE_PREFIX = "STAGING-ONLY: "
_BLOCKING_SEVERITIES = (Severity.WARNING, Severity.ERROR)
_SCALE_CHANGING = (CorporateActionType.SPLIT, CorporateActionType.BONUS, CorporateActionType.OTHER)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    archive: dict = field(default_factory=dict)
    scenario: dict = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"check": name, "status": status, "detail": detail})

    def expect(self, name: str, condition: bool, detail: str = "") -> bool:
        self.add(name, "PASS" if condition else "FAIL", detail)
        return condition

    @property
    def ok(self) -> bool:
        return all(c["status"] != "FAIL" for c in self.checks)

    @property
    def order_flow_exercised(self) -> bool:
        return any(c["check"] == "exit_filled_on_expected_bar" and c["status"] == "PASS" for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "order_flow_exercised": self.order_flow_exercised,
            "archive": self.archive, "scenario": self.scenario, "checks": self.checks,
            "summary": {s: sum(1 for c in self.checks if c["status"] == s) for s in ("PASS", "FAIL", "NOT_EXERCISED")},
        }


def staging_settings(base: Settings, staging_db: str, schedule_path: str, approval_reference: str,
                     participation: str | None = None) -> Settings:
    if not staging_db.endswith("_staging") or staging_db in (base.db_name, base.db_test_name):
        raise SystemExit("refused: the staging database name must end with '_staging' and differ from DB_NAME/DB_TEST_NAME")
    reference = approval_reference.strip()
    if not reference:
        raise SystemExit("refused: an approval reference is required")
    if not reference.startswith("STAGING-ONLY:"):
        reference = f"{STAGING_REFERENCE_PREFIX}{reference}"
    settings = base.model_copy(update={
        "db_name": staging_db,
        "execution_mode": "simulation_provider_native",
        "execution_approval_reference": reference,
        "execution_approved_providers": [base.marketdata_provider],
        "execution_approved_price_bases": [],
        "exchange_cost_schedule_path": schedule_path,
        "exchange_max_volume_participation": D(participation) if participation else None,
    })
    settings._staging_validator_process = True  # in-process only; see Settings.is_staging_run
    return settings


# ---- scenario planning (pure: no database, no clock) ----------------------------------------------------------


@dataclass(frozen=True)
class ArchivedBar:
    """One canonical archived bar, as the price gate sees it."""

    id: int
    instrument_id: int
    start: datetime
    end: datetime
    session_date: date
    open: Decimal
    close: Decimal
    volume: int
    observed_at: datetime  # when Agent-X recorded it (always after `end` for real data)
    blocked: str | None = None  # why the gate would refuse it (data-quality issue, corporate actions), if it would

    @property
    def available_at(self) -> datetime:
        """The earliest simulated time at which this bar may be used: it has ended AND been recorded."""
        return max(self.end, self.observed_at)

    @property
    def recording_lag(self) -> timedelta:
        return self.observed_at - self.end


class BarSeries:
    """One instrument's canonical bars, oldest first, with the lookups the gate and the exchange perform."""

    def __init__(self, bars: Iterable[ArchivedBar]):
        self.bars: list[ArchivedBar] = sorted(bars, key=lambda b: b.start)
        self._ends = [b.end for b in self.bars]
        self._by_start = {b.start: b for b in self.bars}

    def reference_at(self, at: datetime) -> ArchivedBar | None:
        """The latest bar that has ended and been recorded by `at` (as `CandleBarSource.latest`). Bars recorded
        after `at` are invisible, however old they are: nothing from the future is used."""
        for b in reversed(self.bars[: bisect.bisect_right(self._ends, at)]):  # newest first
            if b.observed_at <= at:
                return b
        return None

    def bar_starting(self, start: datetime) -> ArchivedBar | None:
        return self._by_start.get(start)


def fill_slot(accepted_at: datetime, step_seconds: int) -> datetime:
    """Start of the bar a MARKET order accepted at `accepted_at` fills on: the first bar boundary at or after
    acceptance (bars are aligned to the session grid; the same rule as `MockExchange.process_open_orders`)."""
    ts = int(accepted_at.timestamp())
    return datetime.fromtimestamp(-(-ts // step_seconds) * step_seconds, UTC)


@dataclass(frozen=True)
class Leg:
    """One MARKET order: decided (and accepted) at `decided_at` on `reference`, filled on `fill`."""

    decided_at: datetime
    reference: ArchivedBar
    fill: ArchivedBar

    @property
    def processed_at(self) -> datetime:
        """When processing can first see the fill bar."""
        return self.fill.available_at

    def as_dict(self) -> dict:
        return {
            "decided_at": self.decided_at.isoformat(),
            "reference_bar": f"candle:{self.reference.id}", "reference_bar_end": self.reference.end.isoformat(),
            "reference_age_seconds": int((self.decided_at - self.reference.end).total_seconds()),
            "fill_bar": f"candle:{self.fill.id}", "fill_bar_start": self.fill.start.isoformat(),
            "fill_bar_recorded_at": self.fill.observed_at.isoformat(),
            "fill_bar_recording_lag_seconds": int(self.fill.recording_lag.total_seconds()),
            "processed_at": self.processed_at.isoformat(),
        }


def plan_leg(series: BarSeries, decided_at: datetime, max_age: timedelta, step_seconds: int) -> tuple[Leg | None, str]:
    """The fill an order accepted at `decided_at` is expected to get, or the reason it cannot get one."""
    ref = series.reference_at(decided_at)
    if ref is None:
        return None, "NO_DATA: no bar had ended and been recorded by the decision time"
    age = decided_at - ref.end
    if age > max_age:
        return None, f"STALE: reference bar age {int(age.total_seconds())}s exceeds {int(max_age.total_seconds())}s"
    if ref.blocked:
        return None, f"REFERENCE_INELIGIBLE: {ref.blocked}"
    slot = fill_slot(decided_at, step_seconds)
    fill = series.bar_starting(slot)
    if fill is None or fill.session_date != session_date_of(decided_at):
        return None, f"FILL_BAR_MISSING: no bar starting {slot.isoformat()} in the decision's session"
    if fill.blocked:
        return None, f"FILL_BAR_INELIGIBLE: {fill.blocked}"
    if fill.volume <= 0:
        return None, "FILL_BAR_INELIGIBLE: zero volume"
    return Leg(decided_at, ref, fill), "ok"


@dataclass(frozen=True)
class Scenario:
    instrument_id: int
    entry: Leg
    exit: Leg
    last_bar: ArchivedBar

    def as_dict(self) -> dict:
        return {"instrument_id": self.instrument_id, "entry": self.entry.as_dict(), "exit": self.exit.as_dict()}


def find_scenario(series_by_instrument: dict[int, BarSeries], max_age: timedelta, step_seconds: int) -> Scenario | None:
    """The most recent entry-then-exit round trip that the archive supports under the gate's rules.

    Decisions are taken when a bar becomes available (`available_at`), so only bars recorded within `max_age` of their
    close can ever be fresh references; a purely backfilled archive yields None."""
    candidates: list[tuple[datetime, int, ArchivedBar]] = []
    for inst_id, series in series_by_instrument.items():
        for b in series.bars:
            if b.available_at - b.end <= max_age and not b.blocked:
                candidates.append((b.available_at, inst_id, b))
    for decided_at, inst_id, _ in sorted(candidates, key=lambda c: (c[0], -c[1]), reverse=True):
        series = series_by_instrument[inst_id]
        entry, _ = plan_leg(series, decided_at, max_age, step_seconds)
        if entry is None:
            continue
        exit_leg, _ = plan_leg(series, entry.processed_at, max_age, step_seconds)
        if exit_leg is None:
            continue
        return Scenario(inst_id, entry, exit_leg, series.bars[-1])
    return None


def longest_fresh_run(series: BarSeries, max_age: timedelta) -> list[ArchivedBar]:
    """The longest run of contiguous bars (same session, no gap on the grid, positive volume, not refused by the
    gate) each recorded within `max_age` of its close: what a live 5-minute ingestion cadence produces."""
    best: list[ArchivedBar] = []
    run: list[ArchivedBar] = []
    for b in series.bars:
        ok = b.recording_lag <= max_age and b.volume > 0 and not b.blocked
        if ok and run and run[-1].end == b.start and run[-1].session_date == b.session_date:
            run.append(b)
        else:
            run = [b] if ok else []
        if len(run) > len(best):
            best = list(run)
    return best


def freshness_report(series_by_instrument: dict[int, BarSeries], max_age: timedelta, min_run: int) -> dict:
    per = {}
    for inst_id, series in sorted(series_by_instrument.items()):
        run = longest_fresh_run(series, max_age)
        per[inst_id] = {
            "longest_fresh_run": len(run),
            "from": run[0].start.isoformat() if run else None,
            "to": run[-1].end.isoformat() if run else None,
            "max_recording_lag_seconds": max((int(b.recording_lag.total_seconds()) for b in run), default=None),
        }
    best = max((v["longest_fresh_run"] for v in per.values()), default=0)
    return {"max_price_age_seconds": int(max_age.total_seconds()), "required_run": min_run, "best_run": best,
            "sufficient": best >= min_run, "instruments": per, **describe_freshness(series_by_instrument, max_age)}


def describe_freshness(series_by_instrument: dict[int, BarSeries], max_age: timedelta) -> dict:
    lags = sorted(int(b.recording_lag.total_seconds()) for s in series_by_instrument.values() for b in s.bars)
    if not lags:
        return {"bars": 0}
    return {
        "bars": len(lags),
        "recorded_within_max_age": sum(1 for x in lags if x <= max_age.total_seconds()),
        "recording_lag_seconds": {"min": lags[0], "median": lags[len(lags) // 2], "max": lags[-1]},
    }


# ---- loading the archive copy ----------------------------------------------------------------------------------


def load_series(s, provider_id: int, timeframe: str) -> dict[int, BarSeries]:
    """Canonical final bars of the provider, with each bar marked if the price gate would refuse it."""
    blocked_bars = set(s.execute(
        select(DataQualityIssue.instrument_id, DataQualityIssue.bar_start_ts)
        .where(DataQualityIssue.severity.in_(_BLOCKING_SEVERITIES), DataQualityIssue.bar_start_ts.is_not(None))
    ).all())
    blocked_batches = set(s.scalars(
        select(DataQualityIssue.batch_id).where(
            DataQualityIssue.issue_type == IssueType.CORPORATE_ACTIONS_UNAVAILABLE, DataQualityIssue.batch_id.is_not(None))
    ).all())
    actions: dict[int, list[date]] = defaultdict(list)
    for inst_id, ex in s.execute(
        select(CorporateAction.instrument_id, CorporateAction.ex_date).where(CorporateAction.action_type.in_(_SCALE_CHANGING))
    ).all():
        actions[inst_id].append(ex)

    rows = s.execute(
        select(Candle.id, Candle.instrument_id, Candle.bar_start_ts, Candle.bar_end_ts, Candle.session_date, Candle.open,
               Candle.close, Candle.volume, Candle.observed_at, Candle.ingestion_batch_id)
        .where(Candle.provider_id == provider_id, Candle.timeframe == timeframe, Candle.is_canonical.is_(True),
               Candle.is_final.is_(True))
    ).all()
    by_inst: dict[int, list[ArchivedBar]] = defaultdict(list)
    for r in rows:
        why = None
        if (r.instrument_id, r.bar_start_ts) in blocked_bars:
            why = "recorded WARNING/ERROR data-quality issue"
        elif r.ingestion_batch_id in blocked_batches:
            why = "corporate actions unavailable for its ingestion batch"
        elif any(r.session_date < ex <= session_date_of(r.observed_at) for ex in actions.get(r.instrument_id, ())):
            why = "split/bonus/other ex-date between the bar and its recording"
        by_inst[r.instrument_id].append(ArchivedBar(
            id=r.id, instrument_id=r.instrument_id, start=r.bar_start_ts, end=r.bar_end_ts, session_date=r.session_date,
            open=r.open, close=r.close, volume=r.volume, observed_at=r.observed_at, blocked=why,
        ))
    return {k: BarSeries(v) for k, v in by_inst.items()}


# ---- the validation run ----------------------------------------------------------------------------------------


def run_staging_validation(factory, settings: Settings) -> Report:
    from app.exchange.portfolio import daily_pnl
    from app.orders.service import OrderService, TradingContext

    rep = Report()
    max_age = timedelta(seconds=settings.execution_max_price_age_seconds)
    step = timeframe_minutes(settings.execution_candle_timeframe.value) * 60
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
        series = load_series(s, provider.id, settings.execution_candle_timeframe.value)
        rep.archive = {
            "provider": provider.code, "candles": total, "canonical": canonical,
            "price_basis": {k.value: v for k, v in bases.items()},
            "data_quality_issues": {k.value: v for k, v in issues.items()},
            "corporate_actions": {k.value: v for k, v in cas.items()},
            "freshness": describe_freshness(series, max_age),
            "max_price_age_seconds": int(max_age.total_seconds()),
        }
        rep.expect("archive_present", total > 0, f"{total} candles")
        rep.expect("all_bars_provider_native",
                   set(bases) <= {PriceBasis.UNVERIFIED_PROVIDER_NATIVE},
                   "every archived bar is UNVERIFIED_PROVIDER_NATIVE (never relabelled)")
        rep.expect("archive_is_final_only",
                   s.scalar(select(func.count()).select_from(Candle).where(Candle.is_final.is_(False))) == 0)

    scenario = find_scenario(series, max_age, step)
    if scenario is None:
        rep.add("scenario", "NOT_EXERCISED",
                "no entry-and-exit round trip whose reference bars were recorded within "
                f"{int(max_age.total_seconds())}s of their close and whose fill bars exist in the same session; "
                "collect intraday data on the approved cadence first")
        return rep
    rep.scenario = scenario.as_dict()
    entry, exit_leg = scenario.entry, scenario.exit
    problems = settings.execution_config_problems(entry.fill.session_date)
    if not rep.expect("configuration_valid_on_scenario_date", not problems, "; ".join(problems) or str(entry.fill.session_date)):
        return rep

    clock = Clock(entry.decided_at)
    before_candles = rep.archive["candles"]
    inst_id = scenario.instrument_id
    stop = (entry.reference.close * D("0.9")).quantize(D("0.01"))

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
        uid, account_id = user.id, account.id

    def ctx(s):
        return TradingContext.build(s, settings, clock)

    def intent(**kw) -> OrderIntent:
        base = dict(client_order_id=f"stg-{uuid.uuid4().hex[:10]}", instrument_id=inst_id, side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=10, source=OrderSource.MANUAL, stop_loss_price=stop)
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
        rep.expect("decision_used_recorded_reference_bar", r.order.decision_price_ref == f"candle:{entry.reference.id}",
                   f"order={r.order.decision_price_ref} expected=candle:{entry.reference.id}")
        rep.expect("order_records_approval_reference",
                   r.order.execution_approval_reference == settings.execution_approval_reference,
                   str(r.order.execution_approval_reference))
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
            ctx(s).exchange.cancel(s.get(VirtualAccount, account_id), huge.order.id, "staging: not part of scenario")
            s.commit()
        order_id = r.order.id

    # No lookahead: just before the fill bar is recorded, processing must not fill (or touch) the order.
    early = entry.processed_at - timedelta(seconds=1)
    if early >= entry.decided_at and session_date_of(early) == session_date_of(entry.decided_at):
        clock.now = early
        with factory() as s:
            ctx(s).exchange.process_open_orders(s.get(VirtualAccount, account_id))
            s.commit()
            o = s.get(Order, order_id)
            rep.expect("no_fill_before_fill_bar_is_recorded",
                       o.status is OrderStatus.PENDING and s.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == order_id)) == 0,
                       f"processed at {early.isoformat()}: status={o.status.value}")
    else:
        rep.add("no_fill_before_fill_bar_is_recorded", "NOT_EXERCISED", "the fill bar was available at the decision time")

    clock.now = entry.processed_at
    with factory() as s:
        c = ctx(s)
        c.exchange.process_open_orders(s.get(VirtualAccount, account_id))
        s.commit()
        fill = s.scalar(select(Fill).where(Fill.order_id == order_id))
        if not rep.expect("fill_on_expected_bar", fill is not None and fill.price_source_ref == f"candle:{entry.fill.id}",
                          f"expected candle:{entry.fill.id} (start {entry.fill.start.isoformat()}); "
                          f"got {fill.price_source_ref if fill else s.get(Order, order_id).cancel_reason}"):
            return rep
        costs = c.exchange.costs_for(s.get(Order, order_id).product, entry.fill.session_date)
        expected_price = costs.slipped_price(OrderSide.BUY, entry.fill.open)
        rep.expect("fill_price_is_fill_bar_open_plus_slippage",
                   fill.price == expected_price and fill.reference_price == entry.fill.open,
                   f"open={entry.fill.open} fill={fill.price} expected={expected_price}")
        rep.expect("fill_provenance", fill.price_basis is PriceBasis.UNVERIFIED_PROVIDER_NATIVE
                   and fill.execution_policy is ExecutionPolicy.SIMULATION_PROVIDER_NATIVE
                   and fill.price_as_of == entry.fill.start and fill.price_observed_at == entry.fill.observed_at
                   and fill.price_observed_at <= fill.executed_at,
                   f"as_of={fill.price_as_of} observed={fill.price_observed_at} booked={fill.executed_at}")
        rep.expect("fill_records_order_approval_reference",
                   fill.execution_approval_reference == settings.execution_approval_reference, str(fill.execution_approval_reference))
        recomputed = costs.fee(OrderSide.BUY, fill.turnover, FeeContext())
        rep.expect("fees_recomputed_independently", recomputed == fill.fee, f"fill={fill.fee} recomputed={recomputed}")
        ledger = sum(s.scalars(select(CashLedgerEntry.amount).where(CashLedgerEntry.user_id == uid)).all(), D(0))
        rep.expect("ledger_reconciles", ledger == -(fill.turnover + fill.fee), f"ledger={ledger}")
        cash = c.portfolio.cash_balance(s.get(VirtualAccount, account_id))
        rep.expect("cash_equals_initial_plus_ledger", cash == D("1000000") + ledger)
        bought = fill.quantity
    with factory() as s:  # restart recovery: a second processing run (fresh session) books nothing new
        ctx(s).exchange.process_open_orders(s.get(VirtualAccount, account_id))
        s.commit()
        rep.expect("restart_recovery_books_nothing_twice",
                   s.scalar(select(func.count()).select_from(Fill).where(Fill.user_id == uid)) == 1)

    clock.now = exit_leg.decided_at
    with factory() as s:
        sell = OrderService(ctx(s)).submit(uid, intent(side=OrderSide.SELL, quantity=bought, stop_loss_price=None))
        s.commit()
        rep.expect("exit_accepted", sell.order.status is OrderStatus.PENDING, sell.order.reject_reason or "")
        sell_id = sell.order.id
    clock.now = exit_leg.processed_at
    with factory() as s:
        ctx(s).exchange.process_open_orders(s.get(VirtualAccount, account_id))
        s.commit()
        sfill = s.scalar(select(Fill).where(Fill.order_id == sell_id))
        if rep.expect("exit_filled_on_expected_bar",
                      sfill is not None and sfill.price_source_ref == f"candle:{exit_leg.fill.id}"
                      and sfill.reference_price == exit_leg.fill.open,
                      f"expected candle:{exit_leg.fill.id}; got {sfill.price_source_ref if sfill else s.get(Order, sell_id).cancel_reason}"):
            pnl = daily_pnl(s, uid, exit_leg.fill.session_date)
            same_day = entry.fill.session_date == exit_leg.fill.session_date
            rep.expect("daily_pnl_matches_fills", pnl.realized_pnl == sfill.realized_pnl and pnl.fills == (2 if same_day else 1),
                       f"daily={pnl.realized_pnl} fill={sfill.realized_pnl}")

    # Stale data: long after the instrument's last recorded bar, the reference is too old and new orders are refused.
    clock.now = scenario.last_bar.available_at + max_age + timedelta(minutes=5)
    with factory() as s:
        stale = OrderService(ctx(s)).submit(uid, intent())
        s.commit()
        rep.expect("stale_data_rejected", stale.order.status is OrderStatus.REJECTED
                   and "STALE" in (stale.order.reject_reason or ""), stale.order.reject_reason or "")

    if settings.exchange_max_volume_participation is None:
        rep.add("partial_fills", "NOT_EXERCISED", "EXCHANGE_MAX_VOLUME_PARTICIPATION not set for this run")
    with factory() as s:
        split = s.scalar(select(CorporateAction).where(
            CorporateAction.instrument_id == inst_id,
            CorporateAction.action_type.in_((CorporateActionType.SPLIT, CorporateActionType.BONUS)),
            CorporateAction.ex_date > entry.fill.session_date))
        if split is None:
            rep.add("frozen_position_recovery", "NOT_EXERCISED",
                    "no split/bonus with an ex-date after the scenario session for this instrument in the archive")
        else:
            rep.add("frozen_position_recovery", "NOT_EXERCISED",
                    f"split {split.id} exists; recovery is covered by integration tests, not replayed on the copy")
        after = s.scalar(select(func.count()).select_from(Candle).where(Candle.provider_id == provider.id))
        rep.expect("archive_unmodified", after == before_candles, f"{after} candles")
    return rep


class ReplayClock:
    """A trading clock that starts at `start` and advances with real time. Used only by `staging-serve` to exercise
    the API on a recorded session; the price gate still hides every bar not yet ended and recorded by this clock, so
    nothing from the future is visible. Never used outside staging."""

    def __init__(self, start: datetime, real_now=None):
        from app.core.clock import utc_now

        self._real = real_now or utc_now
        self._start = start
        self._origin = self._real()

    def __call__(self) -> datetime:
        return self._start + (self._real() - self._origin)


def staging_serve_main(args) -> int:
    """Run the API against the staging copy with simulation enabled in THIS process only (synthetic schedule allowed,
    STAGING-ONLY approval recorded on every order and fill). Bound to 127.0.0.1. For the manual Mock Exchange
    exercise; never for development or a real environment."""
    import uvicorn

    from app.core.clock import utc_now
    from app.main import create_app

    settings = staging_settings(get_settings(), args.staging_db, str(args.schedule), args.approval_reference,
                                args.participation)
    clock = ReplayClock(datetime.fromisoformat(args.replay_from)) if args.replay_from else utc_now
    if clock is not utc_now and clock().tzinfo is None:
        raise SystemExit("refused: --replay-from must be timezone-aware (e.g. 2026-11-12T04:00:00+00:00)")
    problems = settings.execution_config_problems(session_date_of(clock()))
    if problems:
        raise SystemExit("refused: " + "; ".join(problems))
    app = create_app(settings, clock=clock)
    print(json.dumps({
        "staging_db": settings.db_name, "execution_mode": settings.execution_mode,
        "approval_reference": settings.execution_approval_reference, "schedule": str(args.schedule),
        "clock": "replay from " + args.replay_from if args.replay_from else "real time", "listen": f"127.0.0.1:{args.port}",
    }, indent=2))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


def staging_main(args) -> int:
    base = get_settings()
    settings = staging_settings(base, args.staging_db, str(args.schedule), args.approval_reference, args.participation)
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    report = run_staging_validation(make_sessionmaker(engine), settings)
    print(json.dumps(report.as_dict(), indent=2, default=str))
    if not report.ok:
        return 1
    # Nothing failed, but the order flow never ran (no fresh intraday data): not a passing validation.
    return 0 if report.order_flow_exercised else 2
