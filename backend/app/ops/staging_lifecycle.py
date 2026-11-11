"""Manual Mock Exchange order-lifecycle exercise on the staging copy (B9, step 8), through the real HTTP API.

The API runs in-process (FastAPI TestClient) with the staging validator's own settings (simulation in this process
only, synthetic schedule, STAGING-ONLY approval) and a replay clock that is moved to the moments bars were actually
recorded, exactly as the staging validator plans them: a bar is visible only once it has ended AND been recorded, the
reference must be fresh, and an order fills on the first bar starting at or after acceptance. No bar is created,
edited or back-dated; the archive is only read.

What it exercises (each check PASS / FAIL / NOT_EXERCISED with a reason):
  exchange status enabled in simulation mode; stop-loss required; oversized entry resized or rejected (and a resized
  order's reservation released on cancel); a valid DELIVERY buy accepted with the approval reference and a cash
  reservation; idempotent replay and the 409 for a reused client_order_id; fill on the expected bar at its open plus
  synthetic slippage; fees equal to an independent recomputation from the schedule file; reservation released;
  position, ledger, cash, realized and unrealized P&L reconciled; kill switch blocks entries; stale prices block new
  orders; protective-exit evaluation on stale data records a requirement and an alert, and resolves on fresh data;
  exit fill; no duplicate fills after repeated processing.

Stale-then-fresh recovery needs a real gap in the recorded data (for example the overnight gap between two collected
sessions); without one it is reported NOT_EXERCISED, never simulated.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import Settings
from app.db.enums import OrderSide, Product
from app.db.models import CashLedgerEntry, DataProvider, Fill, ProtectiveExitRequirement
from app.db.session import make_engine, make_sessionmaker
from app.exchange.costs import FeeContext, load_cost_schedule
from app.exchange.pricing import session_date_of
from app.marketdata.calendar import timeframe_minutes
from app.ops.staging import BarSeries, Clock, Leg, find_scenario, load_series, plan_leg

D = Decimal
RISK = {
    "max_risk_per_trade_pct": "2", "max_position_allocation_pct": "50", "max_portfolio_exposure_pct": "100",
    "max_daily_loss_pct": "10", "max_drawdown_pct": "20", "max_position_quantity": None, "resize_allowed": True,
}


@dataclass
class LifecycleReport:
    checks: list[dict] = field(default_factory=list)
    plan: dict = field(default_factory=dict)

    def expect(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        return ok

    def skip(self, name: str, detail: str) -> None:
        self.checks.append({"check": name, "status": "NOT_EXERCISED", "detail": detail})

    @property
    def ok(self) -> bool:
        return all(c["status"] != "FAIL" for c in self.checks)

    @property
    def exercised(self) -> bool:
        return any(c["check"] == "exit_filled_on_expected_bar" and c["status"] == "PASS" for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "order_flow_exercised": self.exercised, "plan": self.plan, "checks": self.checks,
                "summary": {s: sum(1 for c in self.checks if c["status"] == s) for s in ("PASS", "FAIL", "NOT_EXERCISED")}}


@dataclass(frozen=True)
class LifecyclePlan:
    instrument_id: int
    entry: Leg
    exit: Leg
    stale_at: datetime | None  # a moment the position is held and the newest recorded bar is too old
    last_available: datetime


def plan_lifecycle(series_by_instrument: dict[int, BarSeries], max_age: timedelta, step: int) -> LifecyclePlan | None:
    """Prefer a round trip that spans a real stale window (newest recorded bar older than `max_age`, then fresh bars
    again), so stale detection and recovery run on recorded data; otherwise any round trip."""
    for inst_id, series in sorted(series_by_instrument.items()):
        bars = series.bars
        for prev, nxt in zip(bars, bars[1:]):
            stale_at = prev.end + max_age + timedelta(seconds=1)
            if not (nxt.available_at > stale_at and nxt.recording_lag <= max_age):
                continue
            exit_leg, _ = plan_leg(series, nxt.available_at, max_age, step)
            if exit_leg is None:
                continue
            for ref in reversed(bars[: bars.index(prev) + 1]):
                if ref.recording_lag > max_age or ref.blocked:
                    continue
                entry, _ = plan_leg(series, ref.available_at, max_age, step)
                if entry is not None and entry.processed_at < stale_at:
                    return LifecyclePlan(inst_id, entry, exit_leg, stale_at, bars[-1].available_at)
    sc = find_scenario(series_by_instrument, max_age, step)
    if sc is None:
        return None
    return LifecyclePlan(sc.instrument_id, sc.entry, sc.exit, None, sc.last_bar.available_at)


def run_lifecycle(settings: Settings, factory=None) -> LifecycleReport:
    from fastapi.testclient import TestClient

    from app.main import create_app

    rep = LifecycleReport()
    if factory is None:
        factory = make_sessionmaker(make_engine(settings.database_url, settings.db_connect_timeout_seconds))
    max_age = timedelta(seconds=settings.execution_max_price_age_seconds)
    step = timeframe_minutes(settings.execution_candle_timeframe.value) * 60
    with factory() as s:
        provider = s.scalar(select(DataProvider).where(DataProvider.code == settings.marketdata_provider))
        series = load_series(s, provider.id, settings.execution_candle_timeframe.value) if provider else {}
    plan = plan_lifecycle(series, max_age, step)
    if plan is None:
        rep.skip("scenario", "no round trip with fresh recorded bars in the archive; collect intraday data first")
        return rep
    rep.plan = {"instrument_id": plan.instrument_id, "entry": plan.entry.as_dict(), "exit": plan.exit.as_dict(),
                "stale_at": plan.stale_at.isoformat() if plan.stale_at else None}
    schedule = load_cost_schedule(settings.exchange_cost_schedule_path)
    clock = Clock(plan.entry.decided_at)
    app = create_app(settings, clock=clock)
    inst = plan.instrument_id
    stop = str((plan.entry.reference.close * D("0.9")).quantize(D("0.01")))

    def body(cid, **kw):
        b = {"client_order_id": cid, "instrument_id": inst, "side": "BUY", "order_type": "MARKET", "quantity": 10,
             "stop_loss_price": stop}
        b.update(kw)
        return {k: v for k, v in b.items() if v is not None}

    with TestClient(app) as c:
        email = f"staging-lifecycle-{uuid.uuid4().hex[:8]}@example.com"
        pw = "staging-lifecycle-" + uuid.uuid4().hex
        reg = c.post("/api/v1/auth/register", json={"email": email, "password": pw, "initial_capital": "1000000.00"})
        if not rep.expect("register", reg.status_code == 201, f"{reg.status_code} {reg.text[:200]}"):
            return rep
        tok = c.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        rep.expect("risk_settings", c.put("/api/v1/risk/settings", json=RISK, headers=h).status_code == 200)
        uid = uuid.UUID(c.get("/api/v1/auth/me", headers=h).json()["id"])

        st = c.get("/api/v1/exchange/status", headers=h).json()
        rep.expect("exchange_status_enabled_simulation_only",
                   st["execution_enabled"] and st["execution_policy"] == "SIMULATION_PROVIDER_NATIVE"
                   and st["execution_approval_reference"] == settings.execution_approval_reference
                   and not st["blocking_reasons"], json.dumps({k: st[k] for k in ("execution_policy", "blocking_reasons")}))

        r = c.post("/api/v1/orders", json=body("lc-nostop", stop_loss_price=None), headers=h).json()
        rep.expect("stop_loss_required", r["order"]["status"] == "REJECTED" and "STOP_LOSS" in (r["order"]["reject_reason"] or ""),
                   r["order"]["reject_reason"] or "")
        r = c.post("/api/v1/orders", json=body("lc-huge", quantity=10_000_000), headers=h).json()
        o = r["order"]
        resized = o["status"] == "PENDING" and o["quantity"] < 10_000_000
        rep.expect("oversized_entry_resized_or_rejected", o["status"] == "REJECTED" or resized,
                   f"{o['status']} {o['quantity']} {r['risk_decision']['outcome']}")
        if resized:
            cx = c.post(f"/api/v1/orders/{o['id']}/cancel", json={"reason": "lifecycle: resized probe"}, headers=h).json()
            rep.expect("cancel_releases_reservation", cx["status"] == "CANCELLED" and D(cx["reserved_cash"]) == 0)

        buy = body("lc-buy-1")
        r1 = c.post("/api/v1/orders", json=buy, headers=h)
        order = r1.json()["order"]
        rep.expect("valid_delivery_buy_accepted", r1.status_code == 201 and order["status"] == "PENDING"
                   and order["product"] == "DELIVERY", f"{r1.status_code} {order['status']} {order.get('reject_reason')}")
        rep.expect("order_approval_reference", order["execution_approval_reference"] == settings.execution_approval_reference)
        rep.expect("decision_on_expected_reference_bar", order["decision_price_ref"] == f"candle:{plan.entry.reference.id}",
                   order["decision_price_ref"] or "")
        reserved = D(order["reserved_cash"])
        p = c.get("/api/v1/portfolio", headers=h).json()
        rep.expect("cash_reserved", reserved > 0 and D(p["reserved_cash"]) == reserved, f"{reserved} {p['reserved_cash']}")
        r2 = c.post("/api/v1/orders", json=buy, headers=h)
        rep.expect("idempotent_replay", r2.status_code == 200 and r2.json()["replayed"] and r2.json()["order"]["id"] == order["id"])
        r3 = c.post("/api/v1/orders", json={**buy, "quantity": 11}, headers=h)
        rep.expect("reused_client_order_id_conflict", r3.status_code == 409, str(r3.status_code))

        clock.now = plan.entry.processed_at
        c.post("/api/v1/exchange/process", headers=h)
        c.post("/api/v1/exchange/process", headers=h)  # a second run books nothing twice
        detail = c.get(f"/api/v1/orders/{order['id']}", headers=h).json()
        fills = detail["fills"]
        if not rep.expect("entry_filled_on_expected_bar", detail["status"] == "FILLED" and len(fills) == 1
                          and fills[0]["price_source_ref"] == f"candle:{plan.entry.fill.id}",
                          f"{detail['status']} {detail.get('cancel_reason')} fills={len(fills)}"):
            return rep
        f = fills[0]
        costs = schedule.resolve(Product.DELIVERY, plan.entry.fill.session_date)
        rep.expect("fill_price_next_eligible_open_plus_slippage",
                   D(f["price"]) == costs.slipped_price(OrderSide.BUY, plan.entry.fill.open)
                   and D(f["reference_price"]) == plan.entry.fill.open, f"{f['price']} open={plan.entry.fill.open}")
        rep.expect("fill_provenance_simulation_only", f["price_basis"] == "UNVERIFIED_PROVIDER_NATIVE"
                   and f["execution_policy"] == "SIMULATION_PROVIDER_NATIVE" and f["price_source_kind"] == "NEXT_BAR_OPEN")
        rep.expect("fill_approval_reference", f["execution_approval_reference"] == settings.execution_approval_reference)
        buy_fee = costs.fee(OrderSide.BUY, D(f["turnover"]), FeeContext())
        rep.expect("fees_from_synthetic_schedule", D(f["fee"]) == buy_fee
                   and f["cost_model"].get("schedule_version") == schedule.version, f"fill={f['fee']} recomputed={buy_fee}")
        rep.expect("reservation_released_after_fill", D(detail["reserved_cash"]) == 0)
        p = c.get("/api/v1/portfolio", headers=h).json()
        pos = next(x for x in p["positions"] if x["instrument_id"] == inst)
        expected_cash = D("1000000.00") - D(f["turnover"]) - D(f["fee"])
        rep.expect("position_quantity", pos["quantity"] == f["quantity"], str(pos["quantity"]))
        rep.expect("cash_reconciles", D(p["cash_balance"]) == expected_cash and D(p["reserved_cash"]) == 0,
                   f"{p['cash_balance']} expected {expected_cash}")
        if pos["mark_price"] is not None:
            rep.expect("unrealized_pnl_reconciles",
                       D(pos["unrealized_pnl"]) == (D(pos["mark_price"]) - D(pos["avg_entry_price"])) * pos["quantity"],
                       f"{pos['unrealized_pnl']} mark={pos['mark_price']} avg={pos['avg_entry_price']}")
        with factory() as s:
            ledger = s.scalar(select(func.coalesce(func.sum(CashLedgerEntry.amount), 0)).where(CashLedgerEntry.user_id == uid))
        rep.expect("ledger_reconciles_after_entry", D(ledger) == -(D(f["turnover"]) + D(f["fee"])), str(ledger))

        r = c.post("/api/v1/risk/kill-switch", json={"engaged": True, "reason": "lifecycle"}, headers=h)
        k = c.post("/api/v1/orders", json=body("lc-kill"), headers=h).json()["order"]
        rep.expect("kill_switch_blocks_entries", r.status_code == 200 and k["status"] == "REJECTED"
                   and "KILL_SWITCH" in (k["reject_reason"] or ""), k["reject_reason"] or "")
        c.post("/api/v1/risk/kill-switch", json={"engaged": False, "reason": "lifecycle done"}, headers=h)

        if plan.stale_at is not None:
            clock.now = plan.stale_at
            stale = c.post("/api/v1/orders", json=body("lc-stale"), headers=h).json()["order"]
            rep.expect("stale_price_blocks_new_orders", stale["status"] == "REJECTED" and "STALE" in (stale["reject_reason"] or ""),
                       stale["reject_reason"] or "")
            run = c.post("/api/v1/exchange/protective-exits", headers=h).json()
            rep.expect("protective_exit_unresolved_on_stale_data", run["submitted_exits"] == [] and len(run["unresolved"]) == 1
                       and run["unresolved"][0]["reason_code"] == "STALE" and run["risk"]["unresolved_protective_exits"] == 1)
            pf = c.get("/api/v1/portfolio", headers=h).json()
            rep.expect("position_reported_unprotected", any(x["protective_exit_unresolved"] for x in pf["positions"]))
            with factory() as s:
                from app.db.models import AuditEvent

                alerts = s.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.user_id == uid, AuditEvent.event_type == "risk.protective_exit_unresolved"))
            rep.expect("protective_exit_alert_recorded", alerts == 1, str(alerts))
            clock.now = plan.exit.decided_at  # fresh data has been recorded again
            run = c.post("/api/v1/exchange/protective-exits", headers=h).json()
            reqs = c.get("/api/v1/risk/protective-exits", headers=h).json()
            rep.expect("protective_exit_recovers_on_fresh_data", run["unresolved"] == [] and len(reqs) == 1
                       and reqs[0]["status"] == "RESOLVED", json.dumps([(q["status"], q["resolution"]) for q in reqs]))
        else:
            rep.skip("protective_exit_stale_and_recovery", "no stale-then-fresh window in the recorded data")
            clock.now = plan.last_available + max_age + timedelta(minutes=5)
            stale = c.post("/api/v1/orders", json=body("lc-stale"), headers=h).json()["order"]
            rep.expect("stale_price_blocks_new_orders", stale["status"] == "REJECTED" and "STALE" in (stale["reject_reason"] or ""),
                       stale["reject_reason"] or "")
            clock.now = plan.exit.decided_at
            if clock.now < plan.entry.processed_at:  # pragma: no cover - guarded by the planner
                rep.skip("exit", "plan inconsistency")
                return rep

        sell = c.post("/api/v1/orders", json=body("lc-sell-1", side="SELL", stop_loss_price=None, quantity=f["quantity"]),
                      headers=h).json()["order"]
        exit_open = [o for o in c.get("/api/v1/orders?status=PENDING", headers=h).json() if o["side"] == "SELL"]
        rep.expect("exit_accepted", sell["status"] == "PENDING" or bool(exit_open), sell.get("reject_reason") or "")
        clock.now = plan.exit.processed_at
        c.post("/api/v1/exchange/process", headers=h)
        c.post("/api/v1/exchange/process", headers=h)
        sells = [o for o in c.get("/api/v1/orders", headers=h).json() if o["side"] == "SELL" and o["status"] == "FILLED"]
        sold = c.get(f"/api/v1/orders/{sells[0]['id']}", headers=h).json() if sells else None
        sf = sold["fills"][0] if sold and sold["fills"] else None
        if not rep.expect("exit_filled_on_expected_bar", sf is not None and sf["price_source_ref"] == f"candle:{plan.exit.fill.id}",
                          sf["price_source_ref"] if sf else "no exit fill"):
            return rep
        xcosts = schedule.resolve(Product.DELIVERY, plan.exit.fill.session_date)
        sell_fee = xcosts.fee(OrderSide.SELL, D(sf["turnover"]), FeeContext())
        rep.expect("exit_fees_from_synthetic_schedule", D(sf["fee"]) == sell_fee, f"fill={sf['fee']} recomputed={sell_fee}")
        realized = (D(sf["price"]) - D(f["price"])) * sf["quantity"]
        rep.expect("realized_pnl_reconciles", D(sf["realized_pnl"]) == realized, f"{sf['realized_pnl']} expected {realized}")
        p = c.get("/api/v1/portfolio", headers=h).json()
        pos = next(x for x in p["positions"] if x["instrument_id"] == inst)
        final_cash = D("1000000.00") - D(f["turnover"]) - D(f["fee"]) + D(sf["turnover"]) - D(sf["fee"])
        rep.expect("position_closed", pos["quantity"] == 0 and D(pos["realized_pnl"]) == realized)
        rep.expect("final_cash_reconciles", D(p["cash_balance"]) == final_cash and D(p["reserved_cash"]) == 0,
                   f"{p['cash_balance']} expected {final_cash}")
        with factory() as s:
            ledger = s.scalar(select(func.coalesce(func.sum(CashLedgerEntry.amount), 0)).where(CashLedgerEntry.user_id == uid))
            per_order = s.execute(select(Fill.order_id, func.count()).where(Fill.user_id == uid).group_by(Fill.order_id)).all()
            open_reqs = s.scalar(select(func.count()).select_from(ProtectiveExitRequirement).where(
                ProtectiveExitRequirement.user_id == uid, ProtectiveExitRequirement.status == "OPEN"))
        rep.expect("ledger_reconciles_after_exit", D(ledger) == final_cash - D("1000000.00"), str(ledger))
        rep.expect("no_duplicate_fills", all(n == 1 for _, n in per_order) and len(per_order) == 2, str(per_order))
        rep.expect("no_open_protective_requirements", open_reqs == 0)
        day = c.get(f"/api/v1/portfolio/pnl/daily?session_date={session_date_of(plan.exit.fill.start).isoformat()}", headers=h)
        rep.expect("daily_pnl_endpoint", day.status_code == 200)
    return rep


def lifecycle_main(args) -> int:
    from app.core.config import get_settings
    from app.ops.staging import staging_settings

    settings = staging_settings(get_settings(), args.staging_db, str(args.schedule), args.approval_reference)
    report = run_lifecycle(settings)
    print(json.dumps(report.as_dict(), indent=2, default=str))
    if not report.ok:
        return 1
    return 0 if report.exercised else 2
