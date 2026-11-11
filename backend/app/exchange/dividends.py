"""Operator-confirmed gross cash dividends (B5).

Policy (owner decision, 2026-11-11):
  * Entitlement: the shares a user held at the close of the last session before the ex-date, i.e. at the start of
    the ex-date. Holdings are replayed from immutable fills (by simulated fill time) and APPLIED split/bonus
    adjustments; positions are never read from mutable state for this.
  * The per-share amount and the payment date are confirmed by an operator with an evidence reference. Yahoo supplies
    only an ex-date and an amount, which may have been split-adjusted later; a confirmed amount that differs from the
    provider record is refused unless the operator explicitly accepts the difference (both values are recorded).
  * The gross amount (entitled quantity x confirmed amount, 6 dp half-even) is credited on the payment date as a
    separate DIVIDEND ledger entry. No tax (TDS) is deducted: a documented limitation.
  * Idempotent: at most one APPLIED credit per user and dividend (partial unique index); re-running returns the
    existing credit. Late-ingested dividends simply appear as pending; nothing depends on when ingestion ran.
  * A holding that spans an unadjusted split/bonus before the ex-date is on an ambiguous basis: refused until the
    corporate action is adjusted.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.enums import AdjustmentStatus, CorporateActionType, DividendCreditStatus, LedgerEntryType, OrderSide
from app.db.models import AuditEvent, CashLedgerEntry, CorporateAction, CorporateActionAdjustment, DividendCredit, Fill
from app.exchange.costs import MONEY_QUANTUM
from app.exchange.pricing import scale_changing_actions_between
from app.exchange.service import ExchangeInvariantError, lock_account, session_bounds, session_date_of

log = logging.getLogger("agentx.dividends")


class DividendError(ValueError):
    """The request cannot be processed at all (not a dividend, unknown action)."""


@dataclass(frozen=True)
class Entitlement:
    user_id: uuid.UUID
    quantity: int
    problem: str | None  # set when the holding's basis is ambiguous


@dataclass(frozen=True)
class DividendResult:
    credit: DividendCredit
    replayed: bool


def holding_at_ex_date_start(db: Session, user_id: uuid.UUID, instrument_id: int, ex_date: date) -> Entitlement:
    """Shares held at the start of `ex_date` (= close of the last session before it), replayed from immutable
    fills ordered by simulated fill time, with APPLIED split/bonus adjustments applied at their ex-dates."""
    cutoff, _ = session_bounds(ex_date)
    fills = db.execute(
        select(Fill.price_as_of, Fill.id, Fill.side, Fill.quantity).where(
            Fill.user_id == user_id, Fill.instrument_id == instrument_id, Fill.price_as_of < cutoff
        )
    ).all()
    if not fills:
        return Entitlement(user_id, 0, None)
    adjustments = db.scalars(
        select(CorporateActionAdjustment).where(
            CorporateActionAdjustment.user_id == user_id,
            CorporateActionAdjustment.instrument_id == instrument_id,
            CorporateActionAdjustment.status == AdjustmentStatus.APPLIED,
            CorporateActionAdjustment.ex_date < ex_date,
        )
    ).all()
    first_session = session_date_of(min(f[0] for f in fills))
    applied_ids = {a.corporate_action_id for a in adjustments}
    unadjusted = [
        a for a in scale_changing_actions_between(db, instrument_id, first_session, ex_date - timedelta(days=1))
        if a.id not in applied_ids
    ]
    if unadjusted:
        a = unadjusted[0]
        return Entitlement(
            user_id, 0,
            f"UNADJUSTED_CORPORATE_ACTION: {a.action_type.value} ex-date {a.ex_date} (corporate action {a.id}) has not "
            "been adjusted; the holding's basis is ambiguous",
        )
    events = [(f[0], 0, f[1], "fill", f) for f in fills]
    events += [(session_bounds(a.ex_date)[0], 1, a.id, "adj", a) for a in adjustments]
    qty = 0
    for _, _, _, kind, item in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        if kind == "fill":
            qty += item[3] if item[2] is OrderSide.BUY else -item[3]
        else:
            if qty != item.original_quantity:
                raise ExchangeInvariantError(
                    f"replayed holding {qty} does not match adjustment {item.id} original quantity {item.original_quantity}"
                )
            qty = item.adjusted_quantity
    if qty < 0:
        raise ExchangeInvariantError("replayed holding is negative")
    return Entitlement(user_id, qty, None)


class DividendService:
    def __init__(self, db: Session, clock: Clock):
        self.db = db
        self.clock = clock

    def _dividend(self, corporate_action_id: int) -> CorporateAction:
        action = self.db.get(CorporateAction, corporate_action_id)
        if action is None:
            raise DividendError(f"corporate action {corporate_action_id} does not exist")
        if action.action_type is not CorporateActionType.DIVIDEND:
            raise DividendError(f"corporate action {corporate_action_id} is a {action.action_type.value}, not a dividend")
        return action

    def entitlements(self, action: CorporateAction) -> list[Entitlement]:
        cutoff, _ = session_bounds(action.ex_date)
        users = self.db.scalars(
            select(Fill.user_id).where(Fill.instrument_id == action.instrument_id, Fill.price_as_of < cutoff)
            .distinct().order_by(Fill.user_id)
        ).all()
        out = []
        for u in users:
            e = holding_at_ex_date_start(self.db, u, action.instrument_id, action.ex_date)
            if e.quantity > 0 or e.problem:
                out.append(e)
        return out

    def pending(self) -> list[tuple[CorporateAction, list[Entitlement]]]:
        """Dividends whose ex-date has passed with entitled users who have no APPLIED credit."""
        today = session_date_of(self.clock())
        out = []
        for action in self.db.scalars(
            select(CorporateAction).where(
                CorporateAction.action_type == CorporateActionType.DIVIDEND, CorporateAction.ex_date <= today
            ).order_by(CorporateAction.ex_date, CorporateAction.id)
        ):
            credited = set(
                self.db.scalars(
                    select(DividendCredit.user_id).where(
                        DividendCredit.corporate_action_id == action.id, DividendCredit.status == DividendCreditStatus.APPLIED
                    )
                )
            )
            waiting = [e for e in self.entitlements(action) if e.user_id not in credited]
            if waiting:
                out.append((action, waiting))
        return out

    def credit(
        self,
        operator_id: uuid.UUID,
        *,
        corporate_action_id: int,
        evidence_reference: str,
        confirmed_amount_per_share: Decimal,
        payment_date: date,
        accept_provider_difference: bool = False,
    ) -> list[DividendResult]:
        """Credit one dividend to every entitled user (refusals are recorded per user). The caller commits."""
        action = self._dividend(corporate_action_id)
        if confirmed_amount_per_share <= 0:
            raise DividendError("the confirmed amount per share must be positive")
        today = session_date_of(self.clock())
        results = []
        for ent in self.entitlements(action):
            account = lock_account(self.db, ent.user_id)
            existing = self.db.scalar(
                select(DividendCredit).where(
                    DividendCredit.user_id == ent.user_id,
                    DividendCredit.corporate_action_id == action.id,
                    DividendCredit.status == DividendCreditStatus.APPLIED,
                )
            )
            if existing is not None:
                results.append(DividendResult(existing, replayed=True))
                continue
            row = DividendCredit(
                user_id=ent.user_id, account_id=account.id, instrument_id=action.instrument_id,
                corporate_action_id=action.id, ex_date=action.ex_date, cum_date=action.ex_date - timedelta(days=1),
                payment_date=payment_date, entitled_quantity=ent.quantity,
                provider_amount_per_share=action.cash_amount, confirmed_amount_per_share=confirmed_amount_per_share,
                gross_amount=Decimal(0), evidence_reference=evidence_reference.strip() or "(none supplied)",
                evidence={
                    "corporate_action_id": action.id, "source": action.source, "provider_id": action.provider_id,
                    "ingestion_batch_id": action.ingestion_batch_id,
                    "observed_at": action.observed_at.isoformat() if action.observed_at else None,
                    "provider_amount_per_share": str(action.cash_amount) if action.cash_amount is not None else None,
                    "accept_provider_difference": accept_provider_difference,
                    "provider_raw": action.provider_raw,
                },
                status=DividendCreditStatus.REJECTED, operator_id=operator_id, credited_at=self.clock(),
            )
            reason = self._refusal(ent, action, evidence_reference, confirmed_amount_per_share, payment_date, today,
                                   accept_provider_difference)
            if reason:
                row.reason = reason
                self.db.add(row)
                self.db.flush()
                results.append(DividendResult(row, replayed=False))
                continue
            row.gross_amount = (confirmed_amount_per_share * ent.quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
            row.status = DividendCreditStatus.APPLIED
            self.db.add(row)
            self.db.flush()
            self.db.add(
                CashLedgerEntry(
                    user_id=ent.user_id, account_id=account.id, dividend_credit_id=row.id,
                    entry_type=LedgerEntryType.DIVIDEND, amount=row.gross_amount,
                )
            )
            self.db.add(
                AuditEvent(
                    user_id=ent.user_id, event_type="dividend.credited",
                    details={"dividend_credit_id": row.id, "corporate_action_id": action.id,
                             "gross_amount": str(row.gross_amount), "operator_id": str(operator_id)},
                )
            )
            self.db.flush()
            log.info("dividend credited", extra={"fields": {"credit_id": row.id, "user_id": str(ent.user_id),
                                                            "gross": str(row.gross_amount)}})
            results.append(DividendResult(row, replayed=False))
        return results

    @staticmethod
    def _refusal(ent, action, evidence_reference, amount, payment_date, today, accept_difference) -> str | None:
        if ent.problem:
            return ent.problem
        if not evidence_reference.strip():
            return "EVIDENCE_REQUIRED: an evidence reference (e.g. the company's record-date announcement) is required"
        if payment_date < action.ex_date:
            return "PAYMENT_DATE_INVALID: the payment date cannot be before the ex-date"
        if payment_date > today:
            return f"PAYMENT_DATE_IN_FUTURE: credit on or after {payment_date.isoformat()}"
        if action.cash_amount is not None and amount != action.cash_amount and not accept_difference:
            return (
                f"AMOUNT_DIFFERS_FROM_PROVIDER: confirmed {amount} vs provider {action.cash_amount}; set "
                "accept_provider_difference with evidence if the provider value is adjusted or wrong"
            )
        return None
