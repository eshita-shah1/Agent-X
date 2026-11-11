"""Phase 4 endpoints: orders, portfolio, risk settings/kill switch, and exchange status.

Identity comes only from the authenticated user; no endpoint accepts a user id. All writes
happen in one transaction that is committed only after the whole operation succeeds.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.trading_schemas import (
    CancelRequest,
    ExchangeStatusResponse,
    FillResponse,
    KillSwitchRequest,
    OrderCreateRequest,
    OrderDetailResponse,
    OrderResponse,
    OrderSubmitResponse,
    PortfolioResponse,
    PositionResponse,
    ProcessResponse,
    RiskDecisionResponse,
    RiskSettingsRequest,
    RiskSettingsResponse,
    RiskStateResponse,
)
from app.core.clock import Clock, utc_now
from app.core.config import Settings
from app.db.enums import OrderSource, OrderStatus
from app.db.models import Fill, Order, Position, RiskDecision, User
from app.db.scoping import owned_select
from app.exchange.costs import CostModel
from app.exchange.service import OrderNotCancellable, OrderNotFound, lock_account
from app.exchange.types import OrderIntent
from app.orders.service import IdempotencyConflict, InvalidOrder, OrderService, TradingContext
from app.risk.service import RiskSettingsInput, RiskStatus

router = APIRouter(tags=["trading"])


def get_clock(request: Request) -> Clock:
    # Tests may install a fixed clock on app.state; production uses the real UTC clock.
    return getattr(request.app.state, "clock", utc_now)


def get_trading(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    clock: Clock = Depends(get_clock),
) -> TradingContext:
    return TradingContext.build(db, settings, clock)


def _order_detail(db: Session, user: User, order: Order) -> OrderDetailResponse:
    fills = db.scalars(owned_select(Fill, user.id).where(Fill.order_id == order.id).order_by(Fill.seq)).all()
    decision = db.scalar(owned_select(RiskDecision, user.id).where(RiskDecision.order_id == order.id))
    return OrderDetailResponse(
        **OrderResponse.model_validate(order).model_dump(),
        fills=[FillResponse.model_validate(f) for f in fills],
        risk_decision=RiskDecisionResponse.model_validate(decision) if decision else None,
    )


# ---- orders ----------------------------------------------------------------------------------


@router.post("/orders", response_model=OrderSubmitResponse, status_code=status.HTTP_201_CREATED)
def submit_order(
    body: OrderCreateRequest,
    response: Response,
    user: User = Depends(get_current_user),
    ctx: TradingContext = Depends(get_trading),
) -> OrderSubmitResponse:
    intent = OrderIntent(
        client_order_id=body.client_order_id,
        instrument_id=body.instrument_id,
        side=body.side,
        order_type=body.order_type,
        quantity=body.quantity,
        source=OrderSource.MANUAL,
        limit_price=body.limit_price,
        stop_loss_price=body.stop_loss_price,
        take_profit_price=body.take_profit_price,
        strategy_id=body.strategy_id,
        strategy_max_risk_pct=body.strategy_max_risk_pct,
    )
    try:
        result = OrderService(ctx).submit(user.id, intent)
    except IdempotencyConflict as e:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except InvalidOrder as e:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    ctx.db.commit()
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return OrderSubmitResponse(
        order=OrderResponse.model_validate(result.order),
        risk_decision=RiskDecisionResponse.model_validate(result.decision) if result.decision else None,
        replayed=result.replayed,
    )


@router.get("/orders", response_model=list[OrderResponse])
def list_orders(
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Order]:
    stmt = owned_select(Order, user.id)
    if status_filter is not None:
        stmt = stmt.where(Order.status == status_filter)
    return list(db.scalars(stmt.order_by(Order.created_at.desc(), Order.id).limit(limit)))


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
def get_order(order_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.scalar(owned_select(Order, user.id).where(Order.id == order_id))
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return _order_detail(db, user, order)


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse)
def cancel_order(
    order_id: uuid.UUID,
    body: CancelRequest | None = None,
    user: User = Depends(get_current_user),
    ctx: TradingContext = Depends(get_trading),
) -> Order:
    account = lock_account(ctx.db, user.id)
    try:
        order = ctx.exchange.cancel(account, order_id, (body or CancelRequest()).reason)
    except OrderNotFound:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found") from None
    except OrderNotCancellable as e:
        ctx.db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Order cannot be cancelled: {e}") from None
    ctx.db.commit()
    return order


# ---- portfolio -------------------------------------------------------------------------------


@router.get("/portfolio", response_model=PortfolioResponse)
def get_portfolio(user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)):
    account = lock_account(ctx.db, user.id)
    valuation = ctx.portfolio.valuation(account)
    every_position = list(ctx.db.scalars(owned_select(Position, user.id).order_by(Position.instrument_id)))
    marks = {v.position.id: v for v in valuation.positions}
    positions = []
    for p in every_position:
        v = marks.get(p.id)
        positions.append(
            PositionResponse(
                instrument_id=p.instrument_id,
                quantity=p.quantity,
                avg_entry_price=p.avg_entry_price,
                realized_pnl=p.realized_pnl,
                fees_paid=p.fees_paid,
                stop_loss_price=p.stop_loss_price,
                take_profit_price=p.take_profit_price,
                opened_at=p.opened_at,
                mark_price=v.mark if v else None,
                market_value=v.market_value if v else (Decimal(0) if p.quantity == 0 else None),
                unrealized_pnl=v.unrealized_pnl if v else (Decimal(0) if p.quantity == 0 else None),
                mark_unavailable_reason=(
                    None
                    if v is None or v.price_check.ok
                    else f"{v.price_check.rejection.value}: {v.price_check.detail}"
                ),
            )
        )
    complete = not valuation.unmarked_instrument_ids
    unrealized = (
        sum((v.unrealized_pnl for v in valuation.positions if v.unrealized_pnl is not None), Decimal(0))
        if complete
        else None
    )
    ctx.db.rollback()  # read-only endpoint: release the lock without persisting risk-state side effects
    return PortfolioResponse(
        currency=account.currency,
        initial_capital=account.initial_capital,
        cash_balance=valuation.cash_balance,
        reserved_cash=valuation.reserved_cash,
        available_cash=valuation.available_cash,
        positions_market_value=valuation.positions_market_value,
        equity=valuation.equity,
        gross_exposure=valuation.gross_exposure,
        realized_pnl=sum((p.realized_pnl for p in every_position), Decimal(0)),
        fees_paid=sum((p.fees_paid for p in every_position), Decimal(0)),
        unrealized_pnl=unrealized,
        valuation_complete=complete,
        unmarked_instrument_ids=valuation.unmarked_instrument_ids,
        positions=positions,
    )


@router.get("/portfolio/fills", response_model=list[FillResponse])
def list_fills(
    limit: int = Query(default=200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Fill]:
    return list(db.scalars(owned_select(Fill, user.id).order_by(Fill.executed_at.desc(), Fill.id.desc()).limit(limit)))


# ---- risk ------------------------------------------------------------------------------------


@router.get("/risk/settings", response_model=RiskSettingsResponse)
def get_risk_settings(user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)):
    s = ctx.risk.get_settings(user.id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk settings are not configured; entries are blocked")
    return s


@router.put("/risk/settings", response_model=RiskSettingsResponse)
def put_risk_settings(
    body: RiskSettingsRequest, user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)
):
    saved = ctx.risk.save_settings(user.id, RiskSettingsInput(**body.model_dump()))
    ctx.db.commit()
    ctx.db.refresh(saved)
    return saved


@router.get("/risk/state", response_model=RiskStateResponse)
def get_risk_state(user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)):
    """Refreshes the risk references (peak / day-start equity) before reporting, so risk metrics do not
    depend on an order having been submitted."""
    account = lock_account(ctx.db, user.id)
    status_ = ctx.risk.refresh(account)
    ctx.db.commit()
    return _risk_response(status_)


def _risk_response(s: RiskStatus) -> RiskStateResponse:
    st = s.state
    return RiskStateResponse(
        kill_switch_engaged=st.kill_switch_engaged,
        kill_switch_reason=st.kill_switch_reason,
        kill_switch_changed_at=st.kill_switch_changed_at,
        peak_equity=st.peak_equity,
        day_session_date=st.day_session_date,
        day_start_equity=st.day_start_equity,
        last_equity=st.last_equity,
        last_equity_at=st.last_equity_at,
        equity=s.equity,
        drawdown_pct=s.drawdown_pct.quantize(Decimal("0.0001")) if s.drawdown_pct is not None else None,
        daily_loss_pct=s.daily_loss_pct.quantize(Decimal("0.0001")) if s.daily_loss_pct is not None else None,
        breaches=s.breaches,
    )


@router.post("/risk/kill-switch", response_model=RiskStateResponse)
def set_kill_switch(
    body: KillSwitchRequest, user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)
):
    account = lock_account(ctx.db, user.id)
    ctx.risk.set_kill_switch(account, body.engaged, body.reason, ctx.exchange)
    status_ = ctx.risk.refresh(account)
    ctx.db.commit()
    return _risk_response(status_)


@router.get("/risk/decisions", response_model=list[RiskDecisionResponse])
def list_risk_decisions(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RiskDecision]:
    return list(
        db.scalars(
            owned_select(RiskDecision, user.id).order_by(RiskDecision.evaluated_at.desc(), RiskDecision.id.desc()).limit(limit)
        )
    )


# ---- exchange processing and status ---------------------------------------------------------


@router.post("/exchange/process", response_model=ProcessResponse)
def process_orders(user: User = Depends(get_current_user), ctx: TradingContext = Depends(get_trading)):
    """Execute the caller's resting orders against bars finalized since they were accepted, expire DAY orders,
    then refresh the risk references. Explicitly triggered (no scheduler); Phase 5 will call the same service."""
    account = lock_account(ctx.db, user.id)
    changed = ctx.exchange.process_open_orders(account)
    status_ = ctx.risk.refresh(account)
    ctx.db.commit()
    return ProcessResponse(
        changed_orders=[OrderResponse.model_validate(o) for o in changed], risk=_risk_response(status_)
    )




@router.get("/exchange/status", response_model=ExchangeStatusResponse)
def exchange_status(
    _: User = Depends(get_current_user), settings: Settings = Depends(get_settings_dep)
) -> ExchangeStatusResponse:
    costs = CostModel.from_settings(settings)
    blocking = []
    if settings.execution_price_source == "disabled":
        blocking.append("SOURCE_DISABLED: no execution-price source is approved (decision D4)")
    if not settings.execution_approved_price_bases:
        blocking.append("NO_APPROVED_PRICE_BASIS")
    if not settings.execution_approved_providers:
        blocking.append("NO_APPROVED_PROVIDER")
    elif len(settings.execution_approved_providers) != 1:
        blocking.append("AMBIGUOUS_PROVIDER: exactly one provider must be approved")
    if costs is None:
        blocking.append("COSTS_NOT_CONFIGURED: " + ", ".join(CostModel.missing_settings(settings)))
    return ExchangeStatusResponse(
        execution_price_source=settings.execution_price_source,
        execution_model="NEXT_BAR_OPEN: fills at the open of the first finalized bar after acceptance",
        execution_enabled=not blocking,
        approved_price_bases=settings.execution_approved_price_bases,
        approved_providers=settings.execution_approved_providers,
        max_price_age_seconds=settings.execution_max_price_age_seconds,
        costs_configured=costs is not None,
        cost_model=costs.snapshot() if costs else None,
        blocking_reasons=blocking,
    )
