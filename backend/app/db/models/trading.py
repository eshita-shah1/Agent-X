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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UserOwnedMixin
from app.db.enums import (
    LedgerEntryType,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    PriceBasis,
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
        # Defence in depth for decision D4: unverified provider data can never be an execution price.
        CheckConstraint("price_basis <> 'UNVERIFIED_PROVIDER_NATIVE'", name="price_basis_verified"),
        # No lookahead: the observation must exist before the fill that used it.
        CheckConstraint(
            "price_as_of <= executed_at AND price_observed_at <= executed_at", name="price_not_from_future"
        ),
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
    price_as_of: Mapped[datetime] = _ts()
    price_observed_at: Mapped[datetime] = _ts()
    cost_model: Mapped[dict] = mapped_column(JSONB, nullable=False)  # assumptions in force for this fill
    # Per-component fees (brokerage, STT, transaction charge, SEBI fee, stamp duty, GST); migration 0005.
    fee_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    executed_at: Mapped[datetime] = _ts()


class CashLedgerEntry(UserOwnedMixin, Base):
    """Append-only cash movements. Balance = initial_capital + SUM(amount)."""

    __tablename__ = "cash_ledger"
    __table_args__ = (
        UniqueConstraint("fill_id", "entry_type", name="uq_cash_ledger_fill_entry_type"),
        CheckConstraint(
            "(entry_type = 'BUY_SETTLEMENT' AND amount < 0)"
            " OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
            " OR (entry_type = 'FEE' AND amount <= 0)",
            name="amount_sign_matches_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("virtual_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fill_id: Mapped[int] = mapped_column(ForeignKey("fills.id", ondelete="RESTRICT"), nullable=False, index=True)
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
