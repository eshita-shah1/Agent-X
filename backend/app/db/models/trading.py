"""Phase 4 tables: risk configuration and state, orders, fills, the cash ledger and positions.

All are user-owned. Accounting principles:
  * Cash is never stored as a mutable balance. It is derived as
    `virtual_accounts.initial_capital + SUM(cash_ledger.amount)`; the ledger is append-only.
  * Fills, ledger entries and risk decisions are append-only (database triggers, migration 0003).
  * A fill can never carry an UNVERIFIED_PROVIDER_NATIVE price (check constraint), so no code
    path, however it is wired, can execute against unverified provider data.
  * Orders in a terminal state cannot change, and an order's identity columns are immutable
    (trigger), which together with the unique keys below prevents duplicate execution.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Index,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UserOwnedMixin
from app.db.enums import (
    AdjustmentStatus,
    DividendCreditStatus,
    CorporateActionType,
    ExecutionPolicy,
    LedgerEntryType,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    PriceBasis,
    Product,
    RiskOutcome,
)
from app.db.models.market import PRICE, enum_type as _enum

PCT = Numeric(7, 4)  # percentages, e.g. 1.5000 = 1.5 %
MONEY = Numeric(24, 6)  # cash, P&L and fees (INR); kept at price precision, never rounded to paise
AVG_PRICE = Numeric(24, 10)  # volume-weighted average prices keep extra precision to avoid drift
BPS = Numeric(10, 4)


def _ts(nullable: bool = False, default_now: bool = False) -> Mapped:
    if default_now:
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=nullable)
    return mapped_column(DateTime(timezone=True), nullable=nullable)


def _pct_checks(*cols: str) -> tuple[CheckConstraint, ...]:
    return tuple(CheckConstraint(f"{c} > 0 AND {c} <= 100", name=f"{c}_range") for c in cols)


_LIMIT_COLUMNS = (
    "max_risk_per_trade_pct",
    "max_position_allocation_pct",
    "max_portfolio_exposure_pct",
    "max_daily_loss_pct",
    "max_drawdown_pct",
)


class RiskSettings(UserOwnedMixin, Base):
    """User-defined hard risk limits. There are no defaults: until a user saves every limit,
    the risk engine rejects all entries with RISK_SETTINGS_NOT_CONFIGURED."""

    __tablename__ = "risk_settings"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_risk_settings_user_id"),
        *_pct_checks(*_LIMIT_COLUMNS),
        CheckConstraint(
            "max_position_quantity IS NULL OR max_position_quantity > 0", name="max_position_quantity_positive"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    max_risk_per_trade_pct: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    max_position_allocation_pct: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    max_portfolio_exposure_pct: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(PCT, nullable=False)
    # NULL is an explicit user choice meaning "no share-count cap" (value caps still apply).
    max_position_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Whether an oversized proposal is reduced to the largest compliant size or rejected.
    resize_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RiskState(UserOwnedMixin, Base):
    """Kill switch and the equity reference points used by the daily-loss and drawdown limits."""

    __tablename__ = "risk_state"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_risk_state_user_id"),
        CheckConstraint("peak_equity IS NULL OR peak_equity > 0", name="peak_equity_positive"),
        CheckConstraint(
            "(day_session_date IS NULL) = (day_start_equity IS NULL)", name="day_reference_complete"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    kill_switch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_switch_changed_at: Mapped[datetime | None] = _ts(nullable=True)
    peak_equity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    day_session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    day_start_equity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    # Most recent determined equity and when it was computed (migration 0005), so staleness of the risk
    # references is visible instead of silent.
    last_equity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    last_equity_at: Mapped[datetime | None] = _ts(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


_TERMINAL_OR_OPEN = (
    "(status <> 'PENDING' OR filled_quantity = 0)"
    " AND (status <> 'PARTIALLY_FILLED' OR (filled_quantity > 0 AND filled_quantity < quantity))"
    " AND (status <> 'FILLED' OR (filled_quantity = quantity AND quantity > 0))"
)


class Order(UserOwnedMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        # Idempotency: a retried submission with the same client id can never create a second order.
        UniqueConstraint("user_id", "client_order_id", name="uq_orders_user_client_order_id"),
        CheckConstraint("requested_quantity > 0", name="requested_quantity_positive"),
        CheckConstraint("quantity >= 0 AND quantity <= requested_quantity", name="quantity_within_request"),
        CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity", name="filled_within_quantity"),
        CheckConstraint("(order_type = 'LIMIT') = (limit_price IS NOT NULL)", name="limit_price_iff_limit"),
        CheckConstraint("limit_price IS NULL OR limit_price > 0", name="limit_price_positive"),
        CheckConstraint("stop_loss_price IS NULL OR stop_loss_price > 0", name="stop_loss_positive"),
        CheckConstraint("take_profit_price IS NULL OR take_profit_price > 0", name="take_profit_positive"),
        CheckConstraint("status <> 'REJECTED' OR reject_reason IS NOT NULL", name="rejection_has_reason"),
        CheckConstraint("status <> 'REJECTED' OR filled_quantity = 0", name="rejected_never_filled"),
        CheckConstraint("quantity > 0 OR status = 'REJECTED'", name="zero_quantity_only_when_rejected"),
        CheckConstraint("reserved_cash >= 0", name="reserved_cash_non_negative"),
        CheckConstraint(
            "status IN ('PENDING','PARTIALLY_FILLED') OR reserved_cash = 0", name="reservation_released_when_closed"
        ),
        CheckConstraint(_TERMINAL_OR_OPEN, name="status_matches_fill_progress"),
        CheckConstraint("risk_budget IS NULL OR risk_budget >= 0", name="risk_budget_non_negative"),
        # A signal can never come from after the order was accepted (migration 0006).
        CheckConstraint("signal_at <= accepted_at", name="signal_not_after_acceptance"),
        # Migration 0010 (NOT VALID: binds new rows only): an order accepted under an execution policy names the
        # approval that enabled it.
        CheckConstraint(
            "execution_policy IS NULL OR execution_approval_reference IS NOT NULL", name="policy_has_approval_reference"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[OrderSource] = mapped_column(_enum(OrderSource, "order_source"), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType, "order_type"), nullable=False)
    requested_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)  # after risk sizing
    filled_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, "order_status"), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserved_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    avg_fill_price: Mapped[Decimal | None] = mapped_column(AVG_PRICE, nullable=True)
    # Decision time on the trading clock (migration 0005). Only bars starting at or after it can fill the order.
    accepted_at: Mapped[datetime] = _ts()
    # Risk amount approved for a BUY entry; fills are capped so a gap at the next open cannot exceed it.
    risk_budget: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    # Migration 0006: when the strategy/user decided (<= accepted_at), the reference bar that decision used, and the
    # execution policy in force at acceptance (NULL when execution was disabled). Fills must use the same policy.
    signal_at: Mapped[datetime] = _ts()
    decision_price_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_policy: Mapped[ExecutionPolicy | None] = mapped_column(
        _enum(ExecutionPolicy, "execution_policy"), nullable=True
    )
    # Migration 0007: product whose cost rates apply (only DELIVERY is executed today).
    product: Mapped[Product] = mapped_column(_enum(Product, "product"), nullable=False, server_default="DELIVERY")
    # Migration 0010: EXECUTION_APPROVAL_REFERENCE in force when the order was accepted (NULL while execution was
    # disabled, and for orders created before 0010). Immutable (order guard trigger); every fill copies it, and an
    # order is never filled under a different approval (it is cancelled EXECUTION_APPROVAL_CHANGED instead).
    execution_approval_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Fill(UserOwnedMixin, Base):
    """One simulated execution. Append-only; records the exact price observation it used."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("order_id", "seq", name="uq_fills_order_seq"),
        # The same market observation can never fill the same order twice.
        UniqueConstraint("order_id", "price_source_ref", name="uq_fills_order_price_source_ref"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint("price > 0 AND reference_price > 0", name="prices_positive"),
        CheckConstraint("fee >= 0", name="fee_non_negative"),
        CheckConstraint("turnover > 0", name="turnover_positive"),
        # Decision D4, defence in depth (migration 0006 replaces 0004's ck_fills_price_basis_verified): a verified-mode
        # fill can never carry unverified data, and a simulation fill always carries UNVERIFIED_PROVIDER_NATIVE, so a
        # simulated fill can never be presented as a verified one.
        CheckConstraint(
            "(execution_policy = 'VERIFIED_NEXT_BAR_OPEN' AND price_basis <> 'UNVERIFIED_PROVIDER_NATIVE')"
            " OR (execution_policy = 'SIMULATION_PROVIDER_NATIVE' AND price_basis = 'UNVERIFIED_PROVIDER_NATIVE')",
            name="price_basis_matches_policy",
        ),
        # No lookahead: the observation must exist before the fill that used it.
        CheckConstraint(
            "price_as_of <= executed_at AND price_observed_at <= executed_at", name="price_not_from_future"
        ),
        # Migration 0010 (NOT VALID: binds new fills only; older fills predate the column). An insert trigger also
        # requires the value to equal the order's; fills are append-only, so it never changes.
        CheckConstraint("execution_approval_reference IS NOT NULL", name="has_approval_reference"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)  # observed market price
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)  # executed price after slippage
    turnover: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # quantity * price
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    price_source_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    price_basis: Mapped[PriceBasis] = mapped_column(_enum(PriceBasis, "price_basis"), nullable=False)
    execution_policy: Mapped[ExecutionPolicy] = mapped_column(_enum(ExecutionPolicy, "execution_policy"), nullable=False)
    price_as_of: Mapped[datetime] = _ts()
    price_observed_at: Mapped[datetime] = _ts()
    cost_model: Mapped[dict] = mapped_column(JSONB, nullable=False)  # assumptions in force for this fill
    # Per-component fees (brokerage, STT, transaction charge, SEBI fee, stamp duty, GST); migration 0005.
    fee_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Migration 0007: realized P&L of this fill (gross of fees; 0 for BUY), attributed to the *simulated* fill time
    # (`price_as_of`). NULL only for fills booked before 0007.
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    executed_at: Mapped[datetime] = _ts()  # when the simulator booked the fill (processing time)
    # Migration 0010: the order's approval reference (NULL only for fills booked before 0010).
    execution_approval_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)


class CashLedgerEntry(UserOwnedMixin, Base):
    """Append-only cash movements. Balance = initial_capital + SUM(amount). Each entry belongs to exactly one fill
    or (cash-in-lieu, migration 0007) one corporate-action adjustment."""

    __tablename__ = "cash_ledger"
    __table_args__ = (
        UniqueConstraint("fill_id", "entry_type", name="uq_cash_ledger_fill_entry_type"),
        UniqueConstraint("adjustment_id", "entry_type", name="uq_cash_ledger_adjustment_entry_type"),
        UniqueConstraint("dividend_credit_id", "entry_type", name="uq_cash_ledger_dividend_credit_entry_type"),
        CheckConstraint(
            "(entry_type = 'BUY_SETTLEMENT' AND amount < 0)"
            " OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
            " OR (entry_type = 'FEE' AND amount <= 0)"
            " OR (entry_type = 'CASH_IN_LIEU' AND amount > 0)"
            " OR (entry_type = 'DIVIDEND' AND amount > 0)",
            name="amount_sign_matches_type_v3",
        ),
        CheckConstraint(
            "(fill_id IS NOT NULL AND adjustment_id IS NULL AND dividend_credit_id IS NULL"
            " AND entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE'))"
            " OR (fill_id IS NULL AND adjustment_id IS NOT NULL AND dividend_credit_id IS NULL AND entry_type = 'CASH_IN_LIEU')"
            " OR (fill_id IS NULL AND adjustment_id IS NULL AND dividend_credit_id IS NOT NULL AND entry_type = 'DIVIDEND')",
            name="exactly_one_source_v2",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id", ondelete="RESTRICT"), nullable=True, index=True)
    adjustment_id: Mapped[int | None] = mapped_column(
        ForeignKey("corporate_action_adjustments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dividend_credit_id: Mapped[int | None] = mapped_column(
        ForeignKey("dividend_credits.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(_enum(LedgerEntryType, "ledger_entry_type"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)


class Position(UserOwnedMixin, Base):
    """Long-only holding per instrument. Kept (at quantity 0) after closing for realized P&L history."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", name="uq_positions_user_instrument"),
        CheckConstraint("quantity >= 0", name="long_only"),
        CheckConstraint("(quantity = 0) = (avg_entry_price IS NULL)", name="avg_price_iff_open"),
        CheckConstraint("avg_entry_price IS NULL OR avg_entry_price > 0", name="avg_price_positive"),
        CheckConstraint("fees_paid >= 0", name="fees_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    avg_entry_price: Mapped[Decimal | None] = mapped_column(AVG_PRICE, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")  # gross of fees
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    stop_loss_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    opened_at: Mapped[datetime | None] = _ts(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProtectiveExitRequirement(UserOwnedMixin, Base):
    """An open position whose stop-loss/take-profit could not be evaluated because no eligible fresh price existed
    (migration 0011). While OPEN the position is NOT protected by an executed exit; the requirement is re-evaluated on
    every protective-exit run and resolved only by an outcome on fresh data (or the position closing). At most one
    OPEN row per position; resolved rows are frozen and no row is ever deleted (trigger)."""

    __tablename__ = "protective_exit_requirements"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="status_valid"),
        CheckConstraint("stale_trigger IS NULL OR stale_trigger IN ('SL', 'TP')", name="stale_trigger_valid"),
        CheckConstraint(
            "(status = 'OPEN' AND resolved_at IS NULL AND resolution IS NULL)"
            " OR (status = 'RESOLVED' AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="resolution_matches_status",
        ),
        CheckConstraint("checks >= 1", name="checks_positive"),
        Index("uq_protective_exit_requirements_open_per_position", "position_id", unique=True,
              postgresql_where=text("status = 'OPEN'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)  # the price-gate rejection, e.g. STALE
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    # The level the latest (ineligible) observation had already crossed, if any: it is never forgotten.
    stale_trigger: Mapped[str | None] = mapped_column(String(2), nullable=True)
    quote_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_as_of: Mapped[datetime | None] = _ts(nullable=True)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    detected_at: Mapped[datetime] = _ts()
    last_checked_at: Mapped[datetime] = _ts()
    checks: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    resolved_at: Mapped[datetime | None] = _ts(nullable=True)
    # EXIT_SUBMITTED | EXIT_ALREADY_PENDING | NOT_TRIGGERED | POSITION_CLOSED
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)


class RiskDecision(UserOwnedMixin, Base):
    """Append-only log of every risk evaluation (PRD section 4.3)."""

    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"), nullable=False)
    outcome: Mapped[RiskOutcome] = mapped_column(_enum(RiskOutcome, "risk_outcome"), nullable=False)
    proposed_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sizing_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    risk_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    equity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    user_limits: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    strategy_constraints: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    price_check: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime] = _ts()


class CorporateActionAdjustment(UserOwnedMixin, Base):
    """Append-only record of applying (or refusing to apply) a corporate action to one position (migration 0007).

    An APPLIED row is the only way a split/bonus-frozen position resumes: it scales quantity by the ratio and the
    average price by its inverse, preserving cost basis; a fractional entitlement is paid as recorded cash-in-lieu.
    Fills and earlier ledger entries are never modified. At most one APPLIED row exists per position and corporate
    action (partial unique index), so an action can never be applied twice.
    """

    __tablename__ = "corporate_action_adjustments"
    __table_args__ = (
        Index(
            "uq_corporate_action_adjustments_applied_once", "position_id", "corporate_action_id",
            unique=True, postgresql_where=text("status = 'APPLIED'"),
        ),
        CheckConstraint("ratio_numerator > 0 AND ratio_denominator > 0", name="ratio_positive"),
        CheckConstraint("original_quantity > 0", name="original_quantity_positive"),
        CheckConstraint(
            "status <> 'APPLIED' OR (adjusted_quantity IS NOT NULL AND adjusted_avg_price IS NOT NULL "
            "AND adjusted_cost_basis IS NOT NULL AND adjusted_quantity >= 0)",
            name="applied_is_complete",
        ),
        CheckConstraint("status = 'APPLIED' OR reason IS NOT NULL", name="refusal_has_reason"),
        CheckConstraint("fractional_quantity >= 0 AND fractional_quantity < 1", name="fraction_below_one_share"),
        CheckConstraint(
            "(cash_in_lieu IS NULL) = (cash_in_lieu_price IS NULL)", name="cash_in_lieu_amount_and_price_together"
        ),
        CheckConstraint(
            "cash_in_lieu IS NULL OR (cash_in_lieu_source IS NOT NULL AND cash_in_lieu_observed_at IS NOT NULL)",
            name="cash_in_lieu_has_provenance",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"), nullable=False, index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    corporate_action_id: Mapped[int] = mapped_column(
        ForeignKey("corporate_actions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action_type: Mapped[CorporateActionType] = mapped_column(
        _enum(CorporateActionType, "corporate_action_type"), nullable=False
    )
    ratio_numerator: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # new shares
    ratio_denominator: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # old shares
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)  # corporate_actions.source
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)  # operator-supplied evidence
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)  # provenance of the corporate-action record
    status: Mapped[AdjustmentStatus] = mapped_column(_enum(AdjustmentStatus, "adjustment_status"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_avg_price: Mapped[Decimal] = mapped_column(AVG_PRICE, nullable=False)
    original_cost_basis: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    adjusted_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    adjusted_avg_price: Mapped[Decimal | None] = mapped_column(AVG_PRICE, nullable=True)
    adjusted_cost_basis: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fractional_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    cash_in_lieu_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    cash_in_lieu: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    # Migration 0008: where the operator-supplied cash-in-lieu price came from, and when it was published/observed.
    cash_in_lieu_source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cash_in_lieu_observed_at: Mapped[datetime | None] = _ts(nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")  # on the cashed fraction
    original_stop_loss: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    adjusted_stop_loss: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    original_take_profit: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    adjusted_take_profit: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    cancelled_order_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    adjusted_at: Mapped[datetime] = _ts()  # trading clock
    created_at: Mapped[datetime] = _ts(default_now=True)


class DividendCredit(UserOwnedMixin, Base):
    """Append-only record of crediting (or refusing to credit) one cash dividend to one user (migration 0008).

    Entitlement = shares held at the close of the session before the ex-date (replayed from immutable fills and applied
    split adjustments). The per-share amount and payment date are confirmed by an operator with evidence; the gross
    amount is credited as a separate DIVIDEND ledger entry. At most one APPLIED row per user and dividend.
    No tax (TDS) is deducted: a documented limitation.
    """

    __tablename__ = "dividend_credits"
    __table_args__ = (
        Index(
            "uq_dividend_credits_applied_once", "user_id", "corporate_action_id",
            unique=True, postgresql_where=text("status = 'APPLIED'"),
        ),
        CheckConstraint("entitled_quantity >= 0", name="entitled_quantity_non_negative"),
        CheckConstraint("status <> 'APPLIED' OR entitled_quantity > 0", name="applied_has_entitlement"),
        CheckConstraint("confirmed_amount_per_share > 0", name="confirmed_amount_positive"),
        CheckConstraint("status <> 'APPLIED' OR gross_amount > 0", name="applied_amount_positive"),
        CheckConstraint("status = 'APPLIED' OR reason IS NOT NULL", name="refusal_has_reason"),
        CheckConstraint("cum_date < ex_date", name="cum_date_before_ex_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    corporate_action_id: Mapped[int] = mapped_column(
        ForeignKey("corporate_actions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    cum_date: Mapped[date] = mapped_column(Date, nullable=False)  # last session whose closing holding is entitled
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)  # operator-confirmed
    entitled_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_amount_per_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    confirmed_amount_per_share: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[DividendCreditStatus] = mapped_column(
        _enum(DividendCreditStatus, "dividend_credit_status"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    credited_at: Mapped[datetime] = _ts()  # trading clock
    created_at: Mapped[datetime] = _ts(default_now=True)
