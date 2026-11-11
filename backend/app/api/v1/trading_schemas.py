"""Request/response schemas for the Phase 4 risk, order, portfolio and exchange endpoints.

Database models are never returned directly; every response is one of these schemas.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.db.enums import AdjustmentStatus, CorporateActionType, DividendCreditStatus, ExecutionPolicy, OrderSide, Product, OrderSource, OrderStatus, OrderType, PriceBasis, RiskOutcome

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
    # When the decision was made; must be timezone-aware and not in the future. Defaults to the acceptance time.
    signal_at: AwareDatetime | None = None
    # Product whose cost rates apply; only DELIVERY is executed today.
    product: Product = Product.DELIVERY

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
    execution_policy: ExecutionPolicy
    execution_approval_reference: str | None  # the order's approval; NULL only for fills booked before 0010
    realized_pnl: Decimal | None
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
    signal_at: datetime
    decision_price_ref: str | None
    execution_policy: ExecutionPolicy | None
    execution_approval_reference: str | None  # approval in force at acceptance; NULL while execution was disabled
    product: Product
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
    # Positions whose stop-loss/take-profit could not be evaluated on fresh data (not protected by an executed exit).
    unresolved_protective_exits: int = 0


class ProcessResponse(BaseModel):
    changed_orders: list["OrderResponse"]
    risk: RiskStateResponse


class ProtectiveExitRequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: uuid.UUID
    position_id: int
    instrument_id: int
    status: str
    reason_code: str
    detail: str
    stale_trigger: str | None
    quote_ref: str | None
    quote_as_of: datetime | None
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    detected_at: datetime
    last_checked_at: datetime
    checks: int
    resolved_at: datetime | None
    resolution: str | None
    exit_order_id: uuid.UUID | None


class ProtectiveExitRunResponse(BaseModel):
    submitted_exits: list["OrderResponse"]
    unresolved: list[ProtectiveExitRequirementResponse]
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
    # True while an OPEN protective-exit requirement exists: the stop/target could not be evaluated on fresh data,
    # so the position must not be presented as protected by an executed exit.
    protective_exit_unresolved: bool = False


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
    execution_mode: str
    execution_policy: ExecutionPolicy | None
    execution_approval_reference: str | None
    execution_model: str
    execution_enabled: bool
    approved_price_bases: list[PriceBasis]
    approved_providers: list[str]
    max_price_age_seconds: int
    costs_configured: bool
    cost_model: dict | None
    blocking_reasons: list[str]
    disclosures: list[str]


# ---- corporate actions and daily P&L ----------------------------------------------------------------------


class CorporateActionAdjustmentRequest(BaseModel):
    """Operator request: apply one corporate action to every position it freezes."""

    evidence_reference: str = Field(min_length=1, max_length=500)  # e.g. exchange circular / announcement id
    # The operator re-enters the ratio (new : old); it must match the stored corporate-action record exactly.
    confirm_ratio_numerator: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    confirm_ratio_denominator: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    cash_in_lieu_price: Price | None = None  # required only when the entitlement has a fractional share
    cash_in_lieu_source: str | None = Field(default=None, min_length=1, max_length=500)  # where the price came from
    cash_in_lieu_observed_at: AwareDatetime | None = None  # when it was published / observed


class OperatorAdjustmentResult(BaseModel):
    user_id: uuid.UUID
    adjustment: "CorporateActionAdjustmentResponse"
    replayed: bool


class DividendCreditRequest(BaseModel):
    """Operator request: credit one cash dividend to every entitled user (gross, no tax deducted)."""

    evidence_reference: str = Field(min_length=1, max_length=500)  # e.g. company announcement / record-date notice
    confirmed_amount_per_share: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    payment_date: date
    accept_provider_difference: bool = False


class DividendCreditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: uuid.UUID
    instrument_id: int
    corporate_action_id: int
    ex_date: date
    cum_date: date
    payment_date: date
    entitled_quantity: int
    provider_amount_per_share: Decimal | None
    confirmed_amount_per_share: Decimal
    gross_amount: Decimal
    evidence_reference: str
    status: DividendCreditStatus
    reason: str | None
    credited_at: datetime


class OperatorDividendResult(BaseModel):
    credit: DividendCreditResponse
    replayed: bool


class PendingDividendResponse(BaseModel):
    corporate_action_id: int
    instrument_id: int
    ex_date: date
    provider_amount_per_share: Decimal | None
    source: str
    entitled_users: int
    blocked_users: int  # holdings on an ambiguous basis (unadjusted split/bonus)


class PendingOperatorAdjustmentResponse(BaseModel):
    corporate_action_id: int
    instrument_id: int
    action_type: CorporateActionType
    ex_date: date
    ratio_numerator: Decimal | None
    ratio_denominator: Decimal | None
    source: str
    adjustable: bool
    affected_positions: int


class CorporateActionAdjustmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position_id: int
    instrument_id: int
    corporate_action_id: int
    action_type: CorporateActionType
    ratio_numerator: Decimal
    ratio_denominator: Decimal
    ex_date: date
    source: str
    evidence_reference: str
    evidence: dict
    status: AdjustmentStatus
    reason: str | None
    original_quantity: int
    adjusted_quantity: int | None
    original_avg_price: Decimal
    adjusted_avg_price: Decimal | None
    original_cost_basis: Decimal
    adjusted_cost_basis: Decimal | None
    fractional_quantity: Decimal
    cash_in_lieu_price: Decimal | None
    cash_in_lieu: Decimal | None
    cash_in_lieu_source: str | None
    cash_in_lieu_observed_at: datetime | None
    realized_pnl: Decimal
    original_stop_loss: Decimal | None
    adjusted_stop_loss: Decimal | None
    original_take_profit: Decimal | None
    adjusted_take_profit: Decimal | None
    cancelled_order_ids: list
    requested_by: uuid.UUID
    adjusted_at: datetime


class AdjustmentSubmitResponse(BaseModel):
    adjustment: CorporateActionAdjustmentResponse
    replayed: bool


class PendingAdjustmentResponse(BaseModel):
    instrument_id: int
    position_id: int
    quantity: int
    corporate_action_id: int
    action_type: CorporateActionType
    ex_date: date
    ratio_numerator: Decimal | None
    ratio_denominator: Decimal | None
    source: str
    adjustable: bool
    last_attempt_status: AdjustmentStatus | None
    last_attempt_reason: str | None


class DailyPnlResponse(BaseModel):
    session_date: date
    realized_pnl: Decimal
    fees: Decimal
    net_realized_pnl: Decimal
    fills: int
    adjustments: int
    unattributed_legacy_fills: int
    dividend_income: Decimal
    dividends: int


ProcessResponse.model_rebuild()
ProtectiveExitRunResponse.model_rebuild()
OperatorAdjustmentResult.model_rebuild()
