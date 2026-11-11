"""The single entry point for placing orders.

Flow for every order, in one database transaction under the user's account lock:
  1. idempotency (same client_order_id + same request -> the existing order is returned;
     same id + different request -> conflict);
  2. static validation (instrument, quantity, lot/tick);
  3. execution-price eligibility for the instrument;
  4. deterministic risk evaluation (hard limits enforced *before* the exchange);
  5. the decision is logged (risk_decisions, append-only);
  6. rejected -> the order is recorded as REJECTED; approved/resized -> the Mock Exchange,
     which validates again independently and may still reject.

Strategy and agent code (Phases 3 and 5) must submit through `OrderService.submit`; they must
never write orders, fills, positions or ledger entries themselves.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import EXECUTION_MODE_POLICY, OrderSide, OrderSource, OrderStatus, OrderType
from app.db.models import AuditEvent, Instrument, Order, ProtectiveExitRequirement, RiskDecision, VirtualAccount
from app.db.scoping import owned_select
from app.exchange.costs import CostModel, CostSchedule, CostScheduleError, load_cost_schedule
from app.exchange.portfolio import PortfolioReader
from app.exchange.pricing import ExecutionPriceGate, build_price_gate
from app.exchange.service import MockExchange, instrument_problem, lock_account, session_date_of
from app.exchange.types import OrderIntent
from app.marketdata.calendar import timeframe_minutes
from app.risk.engine import RiskResult, TradeProposal, evaluate
from app.risk.service import RiskService

log = logging.getLogger("agentx.orders")


def _protective_trigger(pos, quote) -> str | None:
    """'SL' / 'TP' when the bar's range traded through the position's level (stop first), else None. The exit is a
    MARKET order that fills at the next bar's open; gaps are never filled at the stop price."""
    low = quote.low if quote.low is not None else quote.price
    high = quote.high if quote.high is not None else quote.price
    if pos.stop_loss_price is not None and low <= pos.stop_loss_price:
        return "SL"
    if pos.take_profit_price is not None and high >= pos.take_profit_price:
        return "TP"
    return None


class IdempotencyConflict(ValueError):
    """The client_order_id was already used for a different request."""


class InvalidOrder(ValueError):
    pass


@dataclass(frozen=True)
class SubmitResult:
    order: Order
    replayed: bool  # True when an identical earlier submission was returned
    decision: RiskDecision | None


@dataclass
class TradingContext:
    """Wires the services for one request/transaction."""

    db: Session
    clock: Clock
    gate: ExecutionPriceGate
    costs: CostModel | None
    portfolio: PortfolioReader
    exchange: MockExchange
    risk: RiskService

    cost_schedule: CostSchedule | None = None
    cost_problem: str | None = None  # why `costs` is None

    @classmethod
    def build(cls, db: Session, settings: Settings, clock: Clock, gate: ExecutionPriceGate | None = None):
        gate = gate or build_price_gate(settings, db, clock)
        schedule, problem = None, None
        if settings.exchange_cost_schedule_path:
            try:
                schedule = load_cost_schedule(settings.exchange_cost_schedule_path)
            except CostScheduleError as e:
                problem = str(e)
        else:
            problem = "no cost schedule is configured (EXCHANGE_COST_SCHEDULE_PATH)"
        costs = None
        if schedule is not None:
            try:
                # today's rates for sizing and reservations; fills use the rates of their own simulated date
                costs = schedule.resolve(
                    settings.exchange_product, session_date_of(clock()), settings.exchange_max_volume_participation
                )
            except CostScheduleError as e:
                problem = str(e)
        portfolio = PortfolioReader(db, gate)
        exchange = MockExchange(
            db, gate, schedule, clock, timeframe_minutes(settings.execution_candle_timeframe.value),
            execution_policy=EXECUTION_MODE_POLICY.get(settings.execution_mode)
            if not settings.execution_config_problems(session_date_of(clock()))
            else None,
            max_volume_participation=settings.exchange_max_volume_participation,
            approval_reference=settings.execution_approval_reference,
        )
        return cls(db, clock, gate, costs, portfolio, exchange, RiskService(db, portfolio, clock), schedule, problem)


class OrderService:
    def __init__(self, ctx: TradingContext):
        self.ctx = ctx
        self.db = ctx.db

    def submit(self, user_id: uuid.UUID, intent: OrderIntent) -> SubmitResult:
        account = lock_account(self.db, user_id)
        fingerprint = intent.fingerprint()

        existing = self.db.scalar(owned_select(Order, user_id).where(Order.client_order_id == intent.client_order_id))
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict(f"client_order_id '{intent.client_order_id}' was used for a different order")
            decision = self.db.scalar(owned_select(RiskDecision, user_id).where(RiskDecision.order_id == existing.id))
            return SubmitResult(existing, True, decision)

        instrument = self.db.get(Instrument, intent.instrument_id)
        problem = instrument_problem(instrument, intent.quantity, intent.limit_price)
        if problem:
            raise InvalidOrder(problem)
        if (intent.order_type is OrderType.LIMIT) != (intent.limit_price is not None):
            raise InvalidOrder("limit_price is required for LIMIT orders and not allowed for MARKET orders")
        if intent.signal_at is not None:
            if intent.signal_at.tzinfo is None:
                raise InvalidOrder("signal_at must be timezone-aware")
            if intent.signal_at > self.ctx.clock():
                raise InvalidOrder("signal_at is in the future")

        price_check = self.ctx.gate.check(intent.instrument_id)
        snapshot = self.ctx.risk.snapshot(account, intent.instrument_id)
        limits = self.ctx.risk.limits(user_id)
        proposal = TradeProposal(
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            reference_price=price_check.quote.price if price_check.ok and price_check.quote else None,
            limit_price=intent.limit_price,
            stop_loss_price=intent.stop_loss_price,
            take_profit_price=intent.take_profit_price,
            strategy_id=intent.strategy_id,
            strategy_max_risk_pct=intent.strategy_max_risk_pct,
            lot_size=instrument.lot_size if instrument else None,
        )
        price_detail = (
            price_check.detail if price_check.ok else f"{price_check.rejection.value}: {price_check.detail}"
        )
        result = evaluate(
            proposal, limits, snapshot, self.ctx.costs, price_check.ok, price_detail, self.ctx.cost_problem
        )
        decision_ref = price_check.quote.source_ref if price_check.quote else None

        if result.approved:
            order = self.ctx.exchange.submit(
                account, intent, fingerprint, result.approved_quantity, risk_budget=result.risk_limit_amount,
                decision_price_ref=decision_ref,
            )
        else:
            order = self.ctx.exchange.record_rejected(
                account, intent, fingerprint, "RISK_REJECTED: " + "; ".join(result.reasons),
                decision_price_ref=decision_ref,
            )
        decision = self._log_decision(account, intent, order, result, limits, snapshot.equity, price_check.as_dict())
        log.info(
            "risk decision",
            extra={
                "fields": {
                    "order_id": str(order.id),
                    "user_id": str(user_id),
                    "instrument_id": intent.instrument_id,
                    "strategy_id": intent.strategy_id,
                    "outcome": result.outcome.value,
                    "proposed": intent.quantity,
                    "approved": result.approved_quantity,
                    "reasons": result.reasons,
                    "order_status": order.status.value,
                }
            },
        )
        return SubmitResult(order, False, decision)

    def run_protective_exits(self, user_id: uuid.UUID) -> list[SubmitResult]:
        """Submit MARKET SELL exits for positions whose eligible price has crossed their stop-loss
        or take-profit. Deterministic and idempotent per observation (the client order id is
        derived from the position and the observation). Not scheduled: Phase 5 orchestration
        decides when to call it.

        When no eligible fresh price exists (stale, missing or ineligible data), the evaluation is not dropped: an OPEN
        `ProtectiveExitRequirement` is recorded (one per position), an audit alert is raised, and the position is
        reported as not protected. No exit is triggered from stale data and no price is fabricated. The next run with
        a fresh eligible price resolves it under the same rule; a crossing already observed on the stale bar is not
        forgotten, so that crossing triggers the exit on recovery even if the fresh bar has moved back."""
        account = lock_account(self.db, user_id)
        now = self.ctx.clock()
        results = []
        open_reqs = {
            r.position_id: r
            for r in self.db.scalars(
                owned_select(ProtectiveExitRequirement, user_id)
                .where(ProtectiveExitRequirement.status == "OPEN")
                .with_for_update()
            )
        }
        positions = self.ctx.portfolio.open_positions(user_id)
        held = {p.id for p in positions}
        for position_id, req in open_reqs.items():
            if position_id not in held:
                self._resolve_requirement(req, now, "POSITION_CLOSED")
        for pos in positions:
            if pos.stop_loss_price is None and pos.take_profit_price is None:
                continue
            req = open_reqs.get(pos.id)
            check = self.ctx.gate.check(pos.instrument_id)
            if not check.ok or check.quote is None:
                self._record_unevaluated(account, pos, check, req, now)
                continue
            trigger = _protective_trigger(pos, check.quote)
            if trigger is None and req is not None and req.stale_trigger:
                trigger = req.stale_trigger  # observed on the stale bar: still owed
            if trigger is None:
                if req is not None:
                    self._resolve_requirement(req, now, "NOT_TRIGGERED")
                continue
            sellable = self.ctx.risk.snapshot(account, pos.instrument_id).sellable_quantity
            if sellable <= 0:  # an exit for the whole position is already resting
                if req is not None:
                    self._resolve_requirement(req, now, "EXIT_ALREADY_PENDING")
                continue
            intent = OrderIntent(
                client_order_id=f"{trigger}-{pos.id}-{check.quote.source_ref}"[:64],
                instrument_id=pos.instrument_id,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=sellable,
                source=OrderSource.RISK_EXIT,
                signal_at=check.quote.as_of,  # the bar whose range triggered the exit
            )
            result = self.submit(user_id, intent)
            results.append(result)
            if req is not None:
                if result.order.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED):
                    self._resolve_requirement(req, now, "EXIT_SUBMITTED", exit_order_id=result.order.id)
                else:  # the exit itself was refused (e.g. a frozen position): the requirement stays open
                    req.last_checked_at = now
                    req.checks += 1
                    req.detail = f"exit refused: {result.order.reject_reason}"
        self.db.flush()
        return results

    def _record_unevaluated(self, account: VirtualAccount, pos, check, req, now) -> None:
        stale_trigger = _protective_trigger(pos, check.quote) if check.quote is not None else None
        reason = check.rejection.value if check.rejection else "NO_DATA"
        if req is None:
            req = ProtectiveExitRequirement(
                user_id=account.user_id, account_id=account.id, position_id=pos.id, instrument_id=pos.instrument_id,
                status="OPEN", reason_code=reason, detail=check.detail, stale_trigger=stale_trigger,
                quote_ref=check.quote.source_ref if check.quote else None,
                quote_as_of=check.quote.as_of if check.quote else None,
                stop_loss_price=pos.stop_loss_price, take_profit_price=pos.take_profit_price,
                detected_at=now, last_checked_at=now, checks=1,
            )
            self.db.add(req)
            self.db.flush()
            self.db.add(AuditEvent(
                user_id=account.user_id, event_type="risk.protective_exit_unresolved",
                details={
                    "requirement_id": req.id, "position_id": pos.id, "instrument_id": pos.instrument_id,
                    "reason": reason, "detail": check.detail, "stale_trigger": stale_trigger,
                    "quote_ref": req.quote_ref, "detected_at": now.isoformat(),
                },
            ))
            log.error(
                "protective exit unresolved: no eligible fresh price",
                extra={"fields": {"user_id": str(account.user_id), "position_id": pos.id, "requirement_id": req.id,
                                  "reason": reason, "stale_trigger": stale_trigger}},
            )
            return
        req.last_checked_at = now
        req.checks += 1
        req.detail = check.detail
        if stale_trigger and not req.stale_trigger:
            req.stale_trigger = stale_trigger
            req.quote_ref = check.quote.source_ref
            req.quote_as_of = check.quote.as_of

    def _resolve_requirement(self, req, now, resolution: str, exit_order_id=None) -> None:
        req.status = "RESOLVED"
        req.resolution = resolution
        req.resolved_at = now
        req.last_checked_at = now
        req.exit_order_id = exit_order_id
        self.db.flush()
        self.db.add(AuditEvent(
            user_id=req.user_id, event_type="risk.protective_exit_resolved",
            details={"requirement_id": req.id, "position_id": req.position_id, "resolution": resolution,
                     "exit_order_id": str(exit_order_id) if exit_order_id else None, "resolved_at": now.isoformat()},
        ))

    def _log_decision(
        self,
        account: VirtualAccount,
        intent: OrderIntent,
        order: Order,
        result: RiskResult,
        limits,
        equity,
        price_check: dict,
    ) -> RiskDecision:
        decision = RiskDecision(
            user_id=account.user_id,
            order_id=order.id,
            instrument_id=intent.instrument_id,
            strategy_id=intent.strategy_id,
            side=intent.side,
            outcome=result.outcome,
            proposed_quantity=intent.quantity,
            approved_quantity=result.approved_quantity,
            sizing_price=result.sizing_price,
            stop_loss_price=intent.stop_loss_price,
            risk_amount=result.risk_amount,
            equity=equity,
            user_limits=limits.as_dict() if limits else None,
            strategy_constraints={
                "strategy_id": intent.strategy_id,
                "strategy_max_risk_pct": str(intent.strategy_max_risk_pct)
                if intent.strategy_max_risk_pct is not None
                else None,
                "effective_risk_pct": str(result.effective_risk_pct) if result.effective_risk_pct is not None else None,
            },
            checks=[c.as_dict() for c in result.checks],
            reasons=result.reasons,
            price_check=price_check,
            evaluated_at=self.ctx.clock(),
        )
        self.db.add(decision)
        self.db.flush()
        return decision
