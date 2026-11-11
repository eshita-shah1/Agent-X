"""Operator-only endpoints: corporate-action adjustments and dividend credits, applied system-wide.

Operators are users with `users.is_operator`, which can only be set on the server (`python -m app.ops.cli
set-operator`). Every denied attempt is audited. Operators act on corporate-action records, never on a user's
positions directly: each affected user's adjustment or credit is computed and recorded separately and atomically.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db
from app.api.v1.trading import get_trading
from app.api.v1.trading_schemas import (
    CorporateActionAdjustmentRequest,
    CorporateActionAdjustmentResponse,
    DividendCreditRequest,
    DividendCreditResponse,
    OperatorAdjustmentResult,
    OperatorDividendResult,
    PendingDividendResponse,
    PendingOperatorAdjustmentResponse,
    ProtectiveExitRequirementResponse,
)
from app.db.models import AuditEvent, ProtectiveExitRequirement, User
from app.exchange.corporate_actions import ADJUSTABLE, CorporateActionNotFound, CorporateActionService
from app.exchange.dividends import DividendError, DividendService
from app.orders.service import TradingContext

router = APIRouter(prefix="/operator", tags=["operator"])


def require_operator(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    if not user.is_operator:
        db.add(
            AuditEvent(
                user_id=user.id, event_type="operator.access_denied",
                details={"method": request.method, "path": request.url.path},
            )
        )
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator access required")
    return user


@router.get("/corporate-actions/pending", response_model=list[PendingOperatorAdjustmentResponse])
def pending_adjustments(_: User = Depends(require_operator), ctx: TradingContext = Depends(get_trading)):
    out = []
    for action, positions in CorporateActionService(ctx.db, ctx.exchange, ctx.clock).pending_all():
        out.append(
            PendingOperatorAdjustmentResponse(
                corporate_action_id=action.id, instrument_id=action.instrument_id, action_type=action.action_type,
                ex_date=action.ex_date, ratio_numerator=action.ratio_numerator,
                ratio_denominator=action.ratio_denominator, source=action.source,
                adjustable=action.action_type in ADJUSTABLE, affected_positions=len(positions),
            )
        )
    return out


@router.post("/corporate-actions/{corporate_action_id}/adjustments", response_model=list[OperatorAdjustmentResult])
def apply_adjustment(
    corporate_action_id: int,
    body: CorporateActionAdjustmentRequest,
    operator: User = Depends(require_operator),
    ctx: TradingContext = Depends(get_trading),
):
    """Apply a split/bonus to every position it freezes. Each result is APPLIED (or replayed), REJECTED or
    BLOCKED_FRACTIONAL; refusals are recorded and the affected positions stay frozen."""
    try:
        results = CorporateActionService(ctx.db, ctx.exchange, ctx.clock).apply_to_all(
            operator.id, corporate_action_id=corporate_action_id, evidence_reference=body.evidence_reference,
            confirm_ratio_numerator=body.confirm_ratio_numerator,
            confirm_ratio_denominator=body.confirm_ratio_denominator, cash_in_lieu_price=body.cash_in_lieu_price,
            cash_in_lieu_source=body.cash_in_lieu_source, cash_in_lieu_observed_at=body.cash_in_lieu_observed_at,
        )
    except CorporateActionNotFound as e:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    ctx.db.commit()
    return [
        OperatorAdjustmentResult(
            user_id=r.adjustment.user_id, adjustment=CorporateActionAdjustmentResponse.model_validate(r.adjustment),
            replayed=r.replayed,
        )
        for r in results
    ]


@router.get("/dividends/pending", response_model=list[PendingDividendResponse])
def pending_dividends(_: User = Depends(require_operator), ctx: TradingContext = Depends(get_trading)):
    return [
        PendingDividendResponse(
            corporate_action_id=a.id, instrument_id=a.instrument_id, ex_date=a.ex_date,
            provider_amount_per_share=a.cash_amount, source=a.source,
            entitled_users=sum(1 for e in ents if not e.problem), blocked_users=sum(1 for e in ents if e.problem),
        )
        for a, ents in DividendService(ctx.db, ctx.clock).pending()
    ]


@router.post("/dividends/{corporate_action_id}/credits", response_model=list[OperatorDividendResult])
def credit_dividend(
    corporate_action_id: int,
    body: DividendCreditRequest,
    operator: User = Depends(require_operator),
    ctx: TradingContext = Depends(get_trading),
):
    try:
        results = DividendService(ctx.db, ctx.clock).credit(
            operator.id, corporate_action_id=corporate_action_id, evidence_reference=body.evidence_reference,
            confirmed_amount_per_share=body.confirmed_amount_per_share, payment_date=body.payment_date,
            accept_provider_difference=body.accept_provider_difference,
        )
    except DividendError as e:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    ctx.db.commit()
    return [
        OperatorDividendResult(credit=DividendCreditResponse.model_validate(r.credit), replayed=r.replayed)
        for r in results
    ]


@router.get("/protective-exits/unresolved", response_model=list[ProtectiveExitRequirementResponse])
def unresolved_protective_exits(_: User = Depends(require_operator), db: Session = Depends(get_db)):
    """Alert view: every user's positions whose stop-loss/take-profit could not be evaluated on fresh data. They are
    not protected by an executed exit until resolved (a protective-exit run on fresh data)."""
    return list(db.scalars(
        select(ProtectiveExitRequirement)
        .where(ProtectiveExitRequirement.status == "OPEN")
        .order_by(ProtectiveExitRequirement.detected_at, ProtectiveExitRequirement.id)
    ))
