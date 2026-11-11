"""Corporate-action recovery (B4): an auditable, append-only adjustment that un-freezes a split/bonus position.

Workflow: detect (a split/bonus/other ex-date after a position's simulated open date freezes it) -> validate the
corporate-action evidence -> create an adjustment event -> apply it atomically -> verify -> resume valuation and
exits.

Adjustment arithmetic (ratio = new shares : old shares = numerator : denominator, factor = numerator / denominator):
    entitlement        = quantity x factor                    (exact Decimal)
    adjusted quantity  = floor(entitlement)                   (shares are whole)
    fractional         = entitlement - adjusted quantity      (0 <= fractional < 1)
    adjusted average   = average / factor                     (10 dp, half-even)
    cost basis         = adjusted quantity x adjusted average + fractional x adjusted average  ~= original cost basis
    stop / take-profit = level / factor                       (6 dp, half-even)
Cash is unchanged, except for an explicitly recorded cash-in-lieu: `cash_in_lieu = fractional x cash_in_lieu_price`
(rounded down to 6 dp), with realized P&L = cash_in_lieu - fractional x adjusted average.

Fractional entitlements are never rounded away silently: without an operator-supplied cash-in-lieu price the attempt
is recorded as BLOCKED_FRACTIONAL and the position stays frozen.

Safeguards: idempotent (a partial unique index allows one APPLIED row per position and action; re-applying returns
the existing row); actions apply in ex-date order; fills and earlier ledger entries are never modified; the ratio must
be re-entered by the operator and match the stored record; evidence is required; OTHER-type actions are never
auto-adjusted; the whole adjustment is one transaction and is verified before it commits; refusals are recorded.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.enums import OPEN_ORDER_STATUSES, AdjustmentStatus, CorporateActionType, LedgerEntryType
from app.db.models import (
    AuditEvent,
    CashLedgerEntry,
    CorporateAction,
    CorporateActionAdjustment,
    Order,
    Position,
    VirtualAccount,
)
from app.db.scoping import owned_select
from app.exchange.accounting import AVG_QUANTUM
from app.exchange.costs import MONEY_QUANTUM, PRICE_QUANTUM
from app.exchange.portfolio import unadjusted_action
from app.exchange.service import ExchangeInvariantError, MockExchange, lock_account, session_date_of

log = logging.getLogger("agentx.corporate_actions")

ADJUSTABLE = (CorporateActionType.SPLIT, CorporateActionType.BONUS)


class CorporateActionNotFound(LookupError):
    pass


@dataclass(frozen=True)
class PendingAdjustment:
    position: Position
    action: CorporateAction
    adjustable: bool
    last_attempt: CorporateActionAdjustment | None


@dataclass(frozen=True)
class AdjustmentResult:
    adjustment: CorporateActionAdjustment
    replayed: bool  # the action had already been applied to this position; nothing changed


class CorporateActionService:
    def __init__(self, db: Session, exchange: MockExchange, clock: Clock):
        self.db = db
        self.exchange = exchange
        self.clock = clock

    def pending(self, user_id: uuid.UUID) -> list[PendingAdjustment]:
        """Frozen positions and the corporate action each one is waiting for (the earliest unapplied ex-date)."""
        today = session_date_of(self.clock())
        out = []
        for pos in self.exchange.portfolio.open_positions(user_id):
            action = unadjusted_action(self.db, pos, today)
            if action is None:
                continue
            last = self.db.scalar(
                owned_select(CorporateActionAdjustment, user_id)
                .where(
                    CorporateActionAdjustment.position_id == pos.id,
                    CorporateActionAdjustment.corporate_action_id == action.id,
                )
                .order_by(CorporateActionAdjustment.id.desc())
                .limit(1)
            )
            out.append(PendingAdjustment(pos, action, action.action_type in ADJUSTABLE, last))
        return out

    def adjust(
        self,
        account: VirtualAccount,
        *,
        instrument_id: int,
        corporate_action_id: int,
        evidence_reference: str,
        confirm_ratio_numerator: Decimal,
        confirm_ratio_denominator: Decimal,
        cash_in_lieu_price: Decimal | None = None,
        cash_in_lieu_source: str | None = None,
        cash_in_lieu_observed_at: datetime | None = None,
        requested_by: uuid.UUID | None = None,
    ) -> AdjustmentResult:
        """Validate and apply one corporate action to one position, atomically. The caller commits.

        Returns an APPLIED row (or the existing one, `replayed=True`), or a recorded REJECTED / BLOCKED_FRACTIONAL row
        that leaves the position frozen. Raises CorporateActionNotFound for an unknown position/action."""
        lock_account(self.db, account.user_id)
        position = self.db.scalar(
            owned_select(Position, account.user_id).where(Position.instrument_id == instrument_id).with_for_update()
        )
        action = self.db.get(CorporateAction, corporate_action_id)
        if position is None or action is None or action.instrument_id != instrument_id:
            raise CorporateActionNotFound("no position and corporate action match this instrument")
        if position.quantity <= 0 or position.avg_entry_price is None:
            raise CorporateActionNotFound("there is no open position in this instrument to adjust")

        existing = self.db.scalar(
            owned_select(CorporateActionAdjustment, account.user_id).where(
                CorporateActionAdjustment.position_id == position.id,
                CorporateActionAdjustment.corporate_action_id == action.id,
                CorporateActionAdjustment.status == AdjustmentStatus.APPLIED,
            )
        )
        if existing is not None:
            return AdjustmentResult(existing, replayed=True)  # idempotent: never applied twice

        today = session_date_of(self.clock())
        base = self._base_row(account, position, action, evidence_reference)
        base.requested_by = requested_by or account.user_id
        base.cash_in_lieu_source = (cash_in_lieu_source or "").strip() or None
        base.cash_in_lieu_observed_at = cash_in_lieu_observed_at

        def refuse(reason: str, status: AdjustmentStatus = AdjustmentStatus.REJECTED) -> AdjustmentResult:
            base.status = status
            base.reason = reason
            self.db.add(base)
            self.db.flush()
            log.info("corporate action adjustment refused", extra={"fields": self._log(base)})
            return AdjustmentResult(base, replayed=False)

        pending = unadjusted_action(self.db, position, today)
        if pending is None or pending.id != action.id:
            if pending is None:
                return refuse("NOT_APPLICABLE: this action does not affect the open position")
            return refuse(
                f"OUT_OF_ORDER: apply corporate action {pending.id} (ex-date {pending.ex_date}) first"
            )
        if action.action_type not in ADJUSTABLE:
            return refuse(f"NOT_ADJUSTABLE: {action.action_type.value} actions need a manual procedure")
        if not evidence_reference.strip():
            return refuse("EVIDENCE_REQUIRED: an evidence reference (e.g. exchange circular) is required")
        num, den = action.ratio_numerator, action.ratio_denominator
        if num is None or den is None or num <= 0 or den <= 0:
            return refuse("RATIO_MISSING: the corporate-action record has no valid ratio")
        if confirm_ratio_numerator != num or confirm_ratio_denominator != den:
            return refuse(
                f"RATIO_MISMATCH: confirmed {confirm_ratio_numerator}:{confirm_ratio_denominator} "
                f"does not match recorded {num}:{den}"
            )

        factor = num / den
        qty = position.quantity
        avg = position.avg_entry_price
        assert avg is not None
        entitlement = Decimal(qty) * factor
        whole = int(entitlement.to_integral_value(rounding=ROUND_FLOOR))
        fraction = entitlement - whole
        new_avg = (avg / factor).quantize(AVG_QUANTUM, rounding=ROUND_HALF_EVEN)
        base.adjusted_quantity = whole
        base.adjusted_avg_price = new_avg
        base.adjusted_cost_basis = (new_avg * whole).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        base.fractional_quantity = fraction
        if fraction > 0 and cash_in_lieu_price is None:
            base.adjusted_quantity = base.adjusted_avg_price = base.adjusted_cost_basis = None
            base.cash_in_lieu_source = base.cash_in_lieu_observed_at = None
            return refuse(
                f"CASH_IN_LIEU_PRICE_REQUIRED: fractional entitlement of {fraction} share(s); supply the cash-in-lieu "
                "price from the corporate-action evidence",
                AdjustmentStatus.BLOCKED_FRACTIONAL,
            )
        if fraction == 0 and cash_in_lieu_price is not None:
            base.cash_in_lieu_source = base.cash_in_lieu_observed_at = None
            return refuse("CASH_IN_LIEU_NOT_APPLICABLE: the entitlement has no fractional share")
        if fraction > 0 and (base.cash_in_lieu_source is None or cash_in_lieu_observed_at is None):
            base.adjusted_quantity = base.adjusted_avg_price = base.adjusted_cost_basis = None
            base.cash_in_lieu_source = base.cash_in_lieu_observed_at = None
            return refuse(
                "CASH_IN_LIEU_PROVENANCE_REQUIRED: the cash-in-lieu price needs its source and the time it was "
                "published or observed; it is never estimated",
                AdjustmentStatus.BLOCKED_FRACTIONAL,
            )
        if cash_in_lieu_observed_at is not None and cash_in_lieu_observed_at.tzinfo is None:
            return refuse("CASH_IN_LIEU_PROVENANCE_INVALID: the observation time must be timezone-aware")

        cil = realized = None
        if fraction > 0:
            assert cash_in_lieu_price is not None
            cil = (fraction * cash_in_lieu_price).quantize(MONEY_QUANTUM, rounding=ROUND_FLOOR)
            realized = (cil - fraction * new_avg).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
            if cil <= 0:
                return refuse("CASH_IN_LIEU_ZERO: the cash-in-lieu amount rounds to zero")
            base.cash_in_lieu_price = cash_in_lieu_price
            base.cash_in_lieu = cil
            base.realized_pnl = realized

        # Verify: cost basis is preserved within the quantization of the average price.
        original_cost = avg * qty
        rebuilt = new_avg * entitlement
        tolerance = entitlement * AVG_QUANTUM + MONEY_QUANTUM
        if abs(rebuilt - original_cost) > tolerance:
            raise ExchangeInvariantError("corporate-action adjustment would not preserve the cost basis")

        def scale(level: Decimal | None) -> Decimal | None:
            return None if level is None else (level / factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)

        base.adjusted_stop_loss = scale(position.stop_loss_price)
        base.adjusted_take_profit = scale(position.take_profit_price)
        # Orders priced on the old basis can never fill on the new one.
        cancelled = []
        for o in self.db.scalars(
            owned_select(Order, account.user_id)
            .where(Order.instrument_id == instrument_id, Order.status.in_(OPEN_ORDER_STATUSES))
            .with_for_update()
        ):
            self.exchange._close_open_order(o, f"CORPORATE_ACTION_ADJUSTMENT: corporate action {action.id}")
            cancelled.append(str(o.id))
        base.cancelled_order_ids = cancelled
        base.status = AdjustmentStatus.APPLIED
        self.db.add(base)
        self.db.flush()

        position.quantity = whole
        position.avg_entry_price = new_avg if whole > 0 else None
        position.stop_loss_price = base.adjusted_stop_loss if whole > 0 else None
        position.take_profit_price = base.adjusted_take_profit if whole > 0 else None
        if whole == 0:
            position.opened_at = None
        if realized is not None:
            position.realized_pnl = position.realized_pnl + realized
        if cil is not None:
            self.db.add(
                CashLedgerEntry(
                    user_id=account.user_id, account_id=account.id, adjustment_id=base.id,
                    entry_type=LedgerEntryType.CASH_IN_LIEU, amount=cil,
                )
            )
        self.db.add(
            AuditEvent(
                user_id=account.user_id, event_type="corporate_action.adjusted",
                details={"adjustment_id": base.id, "corporate_action_id": action.id, "instrument_id": instrument_id,
                         "evidence_reference": evidence_reference, "operator_id": str(base.requested_by)},
            )
        )
        self.db.flush()

        # Post-condition: this action no longer freezes the position.
        if whole > 0:
            still = unadjusted_action(self.db, position, action.ex_date)
            if still is not None and still.id == action.id:
                raise ExchangeInvariantError("adjustment did not resolve the corporate action")
        log.info("corporate action adjustment applied", extra={"fields": self._log(base)})
        return AdjustmentResult(base, replayed=False)

    def apply_to_all(
        self,
        operator_id: uuid.UUID,
        *,
        corporate_action_id: int,
        evidence_reference: str,
        confirm_ratio_numerator: Decimal,
        confirm_ratio_denominator: Decimal,
        cash_in_lieu_price: Decimal | None = None,
        cash_in_lieu_source: str | None = None,
        cash_in_lieu_observed_at: datetime | None = None,
    ) -> list[AdjustmentResult]:
        """Operator action: apply one corporate action to every position it currently freezes, across all users.
        Each user's adjustment (or refusal) is recorded separately; one transaction (the caller commits)."""
        action = self.db.get(CorporateAction, corporate_action_id)
        if action is None:
            raise CorporateActionNotFound(f"corporate action {corporate_action_id} does not exist")
        today = session_date_of(self.clock())
        results = []
        for user_id in self._affected_users(action, today):
            account = lock_account(self.db, user_id)
            results.append(
                self.adjust(
                    account, instrument_id=action.instrument_id, corporate_action_id=action.id,
                    evidence_reference=evidence_reference, confirm_ratio_numerator=confirm_ratio_numerator,
                    confirm_ratio_denominator=confirm_ratio_denominator, cash_in_lieu_price=cash_in_lieu_price,
                    cash_in_lieu_source=cash_in_lieu_source, cash_in_lieu_observed_at=cash_in_lieu_observed_at,
                    requested_by=operator_id,
                )
            )
        return results

    def _affected_users(self, action: CorporateAction, today) -> list[uuid.UUID]:
        users = []
        for pos in self.db.scalars(
            select(Position).where(Position.instrument_id == action.instrument_id, Position.quantity > 0)
            .order_by(Position.user_id)
        ):
            pending = unadjusted_action(self.db, pos, today)
            if pending is not None and pending.id == action.id:
                users.append(pos.user_id)
        return users

    def pending_all(self) -> list[tuple[CorporateAction, list[Position]]]:
        """Operator view: every corporate action currently freezing at least one position, with those positions."""
        today = session_date_of(self.clock())
        grouped: dict[int, tuple[CorporateAction, list[Position]]] = {}
        for pos in self.db.scalars(select(Position).where(Position.quantity > 0).order_by(Position.user_id)):
            action = unadjusted_action(self.db, pos, today)
            if action is not None:
                grouped.setdefault(action.id, (action, []))[1].append(pos)
        return [grouped[k] for k in sorted(grouped)]

    def _base_row(
        self, account: VirtualAccount, position: Position, action: CorporateAction, evidence_reference: str
    ) -> CorporateActionAdjustment:
        qty = position.quantity
        avg = position.avg_entry_price
        assert qty > 0 and avg is not None
        return CorporateActionAdjustment(
            user_id=account.user_id,
            account_id=account.id,
            position_id=position.id,
            instrument_id=position.instrument_id,
            corporate_action_id=action.id,
            action_type=action.action_type,
            ratio_numerator=action.ratio_numerator if action.ratio_numerator else Decimal(1),
            ratio_denominator=action.ratio_denominator if action.ratio_denominator else Decimal(1),
            ex_date=action.ex_date,
            source=action.source,
            evidence_reference=evidence_reference.strip() or "(none supplied)",
            evidence={
                "corporate_action_id": action.id,
                "source": action.source,
                "provider_id": action.provider_id,
                "ingestion_batch_id": action.ingestion_batch_id,
                "observed_at": action.observed_at.isoformat() if action.observed_at else None,
                "recorded_ratio": (
                    [str(action.ratio_numerator), str(action.ratio_denominator)] if action.ratio_numerator else None
                ),
                "provider_raw": action.provider_raw,
            },
            original_quantity=qty,
            original_avg_price=avg,
            original_cost_basis=(avg * qty).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN),
            fractional_quantity=Decimal(0),
            original_stop_loss=position.stop_loss_price,
            original_take_profit=position.take_profit_price,
            requested_by=account.user_id,
            adjusted_at=self.clock(),
        )

    @staticmethod
    def _log(a: CorporateActionAdjustment) -> dict:
        return {
            "adjustment_id": a.id, "position_id": a.position_id, "corporate_action_id": a.corporate_action_id,
            "status": a.status.value, "reason": a.reason, "original_quantity": a.original_quantity,
            "adjusted_quantity": a.adjusted_quantity,
        }
