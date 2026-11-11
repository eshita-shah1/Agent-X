"""Request/response schemas for the Phase 4 risk, order, portfolio and exchange endpoints.

Database models are never returned directly; every response is one of these schemas.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import OrderSide, OrderSource, OrderStatus, OrderType, PriceBasis, RiskOutcome

Price = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]
Pct = Annotated[Decimal, Field(gt=0, le=100, max_digits=7, decimal_places=4)]
Quantity = Annotated[int, Field(gt=0, le=1_000_000_000)]


# ---- orders ----------------------------------------------------------------------------------


class OrderCreateRequest(BaseModel):
    client_order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:\-]+$")
    instrument_id: int = Field(gt=0)
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    limit_price: Price | None = None
    stop_loss_price: Price | None = None
    take_profit_price: Price | None = None
    strategy_id: str | None = Field(default=None, min_length=1, max_length=64)
    strategy_max_risk_pct: Pct | None = None

    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> "OrderCreateRequest":
        if (self.order_type is OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for LIMIT orders and not allowed for MARKET orders")
        if self.side is OrderSide.SELL and (self.stop_loss_price or self.take_profit_price):
            raise ValueError("stop_loss_price/take_profit_price apply to entries (BUY) only")
        return self


class FillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: uuid.UUID
    instrument_id: int
    seq: int
    side: OrderSide
    quantity: int
    reference_price: Decimal
    price: Decimal
    turnover: Decimal
    fee: Decimal
    price_source_kind: str
    price_source_ref: str
    provider_code: str
    price_basis: PriceBasis
    price_as_of: datetime
    price_observed_at: datetime
    cost_model: dict
    fee_breakdown: dict
    executed_at: datetime


class RiskDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: uuid.UUID
    instrument_id: int
    strategy_id: str | None
    side: OrderSide
    outcome: RiskOutcome
    proposed_quantity: int
    approved_quantity: int
    sizing_price: Decimal | None
    stop_loss_price: Decimal | None
    risk_amount: Decimal | None
    equity: Decimal | None
    user_limits: dict | None
    strategy_constraints: dict
    checks: list
    reasons: list
    price_check: dict
    evaluated_at: datetime


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_order_id: str
    instrument_id: int
    source: OrderSource
    strategy_id: str | None
    side: OrderSide
    order_type: OrderType
    requested_quantity: int
    quantity: int
    filled_quantity: int
    limit_price: Decimal | None
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    status: OrderStatus
    reject_reason: str | None
    cancel_reason: str | None
    reserved_cash: Decimal
    avg_fill_price: Decimal | None
    accepted_at: datetime
    risk_budget: Decimal | None
    created_at: datetime
    updated_at: datetime


class OrderDetailResponse(OrderResponse):
    fills: list[FillResponse]
    risk_decision: RiskDecisionResponse | None


class OrderSubmitResponse(BaseModel):
    order: OrderResponse
    risk_decision: RiskDecisionResponse | None
    replayed: bool


class CancelRequest(BaseModel):
    reason: str = Field(default="cancelled by user", min_length=1, max_length=500)


# ---- risk ----------------------------------------------------------------------------------


class RiskSettingsRequest(BaseModel):
    """Every limit is required; there are no defaults. `max_position_quantity` must be sent
    explicitly and may be null (no share-count cap)."""

    max_risk_per_trade_pct: Pct
    max_position_allocation_pct: Pct
    max_portfolio_exposure_pct: Pct
    max_daily_loss_pct: Pct
    max_drawdown_pct: Pct
    max_position_quantity: Quantity | None
    resize_allowed: bool


class RiskSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_risk_per_trade_pct: Decimal
    max_position_allocation_pct: Decimal
    max_portfolio_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_position_quantity: int | None
    resize_allowed: bool
    created_at: datetime
    updated_at: datetime


class RiskStateResponse(BaseModel):
    kill_switch_engaged: bool
    kill_switch_reason: str | None
    kill_switch_changed_at: datetime | None
    peak_equity: Decimal | None
    day_session_date: date | None
    day_start_equity: Decimal | None
    last_equity: Decimal | None
    last_equity_at: datetime | None
    # Computed on this request (the references above are refreshed first).
    equity: Decimal | None
    drawdown_pct: Decimal | None
    daily_loss_pct: Decimal | None
    breaches: list[str]


class ProcessResponse(BaseModel):
    changed_orders: list["OrderResponse"]
    risk: RiskStateResponse


class KillSwitchRequest(BaseModel):
    engaged: bool
    reason: str = Field(min_length=1, max_length=500)


# ---- portfolio -----------------------------------------------------------------------------


class PositionResponse(BaseModel):
    instrument_id: int
    quantity: int
    avg_entry_price: Decimal | None
    realized_pnl: Decimal
    fees_paid: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    opened_at: datetime | None
    mark_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    mark_unavailable_reason: str | None


class PortfolioResponse(BaseModel):
    currency: str
    initial_capital: Decimal
    cash_balance: Decimal
    reserved_cash: Decimal
    available_cash: Decimal
    positions_market_value: Decimal | None
    equity: Decimal | None
    gross_exposure: Decimal | None
    realized_pnl: Decimal
    fees_paid: Decimal
    unrealized_pnl: Decimal | None
    valuation_complete: bool
    unmarked_instrument_ids: list[int]
    positions: list[PositionResponse]


# ---- exchange status ---------------------------------------------------------------------------


class ExchangeStatusResponse(BaseModel):
    execution_price_source: str
    execution_model: str
    execution_enabled: bool
    approved_price_bases: list[PriceBasis]
    approved_providers: list[str]
    max_price_age_seconds: int
    costs_configured: bool
    cost_model: dict | None
    blocking_reasons: list[str]


ProcessResponse.model_rebuild()
