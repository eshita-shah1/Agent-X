"""Pure pre-trade risk evaluation.

Every rule here is deterministic Python on Decimal values. User limits are hard limits; a
strategy can only tighten them. The result is a decision (APPROVED / RESIZED / REJECTED) with
every check recorded, so the decision log explains itself.

Definitions (all percentages are of current marked equity):
  * per-share risk  = slipped entry price - stop-loss price
  * trade risk      = approved quantity * per-share risk
  * allocation      = value of this instrument's position + open BUY commitments in it
  * exposure        = marked value of all positions + cash committed to open BUY orders
  * drawdown        = (peak equity - equity) / peak equity
  * daily loss      = (equity at first evaluation of the session date - equity) / that equity

Entries (BUY) must carry a stop-loss below the entry price; without it trade risk is
undefined and the entry is rejected. Exits (SELL of a held long position) reduce risk and are
only checked for price eligibility and held quantity; they are allowed while the kill switch
is engaged or a loss limit is breached.
"""

import enum
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from app.db.enums import OrderSide, OrderType, RiskOutcome
from app.exchange.costs import CostModel

HUNDRED = Decimal(100)
ZERO = Decimal(0)


class RiskReason(str, enum.Enum):
    NO_ELIGIBLE_PRICE = "NO_ELIGIBLE_PRICE"
    COSTS_NOT_CONFIGURED = "COSTS_NOT_CONFIGURED"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    RISK_SETTINGS_NOT_CONFIGURED = "RISK_SETTINGS_NOT_CONFIGURED"
    STOP_LOSS_REQUIRED = "STOP_LOSS_REQUIRED"
    STOP_LOSS_NOT_BELOW_ENTRY = "STOP_LOSS_NOT_BELOW_ENTRY"
    TAKE_PROFIT_NOT_ABOVE_ENTRY = "TAKE_PROFIT_NOT_ABOVE_ENTRY"
    EQUITY_UNDETERMINED = "EQUITY_UNDETERMINED"
    MAX_DRAWDOWN_BREACHED = "MAX_DRAWDOWN_BREACHED"
    DAILY_LOSS_LIMIT_BREACHED = "DAILY_LOSS_LIMIT_BREACHED"
    SIZE_EXCEEDS_LIMITS = "SIZE_EXCEEDS_LIMITS"
    RESIZE_NOT_ALLOWED = "RESIZE_NOT_ALLOWED"


@dataclass(frozen=True)
class UserRiskLimits:
    max_risk_per_trade_pct: Decimal
    max_position_allocation_pct: Decimal
    max_portfolio_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_position_quantity: int | None
    resize_allowed: bool

    def as_dict(self) -> dict:
        return {
            "max_risk_per_trade_pct": str(self.max_risk_per_trade_pct),
            "max_position_allocation_pct": str(self.max_position_allocation_pct),
            "max_portfolio_exposure_pct": str(self.max_portfolio_exposure_pct),
            "max_daily_loss_pct": str(self.max_daily_loss_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "max_position_quantity": self.max_position_quantity,
            "resize_allowed": self.resize_allowed,
        }


@dataclass(frozen=True)
class AccountRiskSnapshot:
    kill_switch_engaged: bool
    equity: Decimal | None
    equity_detail: str
    available_cash: Decimal
    gross_exposure: Decimal | None
    peak_equity: Decimal | None
    day_start_equity: Decimal | None
    position_quantity: int  # held quantity of the proposal's instrument
    position_value: Decimal | None  # marked value of that holding
    sellable_quantity: int  # held minus quantity committed to open SELL orders
    open_buy_quantity: int  # unfilled quantity of open BUY orders in this instrument
    open_buy_commitment: Decimal  # cash reserved by open BUY orders in this instrument


@dataclass(frozen=True)
class TradeProposal:
    side: OrderSide
    order_type: OrderType
    quantity: int
    reference_price: Decimal | None  # eligible observed price, None if unavailable
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    strategy_id: str | None = None
    strategy_max_risk_pct: Decimal | None = None
    lot_size: int | None = None


@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str
    cap: int | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail, "cap": self.cap}


@dataclass
class RiskResult:
    outcome: RiskOutcome
    approved_quantity: int
    sizing_price: Decimal | None = None
    risk_amount: Decimal | None = None
    effective_risk_pct: Decimal | None = None
    # Hard per-trade risk limit in money (equity x effective %). The exchange caps fills with it, so a gap
    # at the next open can never make the realized trade risk exceed the user's limit.
    risk_limit_amount: Decimal | None = None
    checks: list[RiskCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.outcome is not RiskOutcome.REJECTED


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR)) if x > 0 else 0


def evaluate(
    proposal: TradeProposal,
    limits: UserRiskLimits | None,
    snapshot: AccountRiskSnapshot,
    costs: CostModel | None,
    price_ok: bool,
    price_detail: str,
) -> RiskResult:
    checks: list[RiskCheck] = []

    def reject(reason: RiskReason, detail: str, **extra) -> RiskResult:
        checks.append(RiskCheck(reason.value, False, detail))
        return RiskResult(RiskOutcome.REJECTED, 0, checks=checks, reasons=[f"{reason.value}: {detail}"], **extra)

    # Stale or unapproved data blocks every new order for the instrument (PRD 7.4 / 11.1).
    if not price_ok or proposal.reference_price is None:
        return reject(RiskReason.NO_ELIGIBLE_PRICE, price_detail)
    checks.append(RiskCheck("price_eligible", True, price_detail))
    if costs is None:
        return reject(RiskReason.COSTS_NOT_CONFIGURED, "fee and slippage assumptions are not configured")

    if proposal.side is OrderSide.SELL:
        if proposal.quantity > snapshot.sellable_quantity:
            return reject(
                RiskReason.INSUFFICIENT_POSITION,
                f"sell {proposal.quantity} exceeds sellable {snapshot.sellable_quantity}; short selling is not supported",
            )
        checks.append(RiskCheck("risk_reducing_exit", True, "sell of a held long position"))
        return RiskResult(
            RiskOutcome.APPROVED, proposal.quantity, sizing_price=proposal.reference_price, checks=checks
        )

    # ---- entries (BUY) -------------------------------------------------------------------
    if snapshot.kill_switch_engaged:
        return reject(RiskReason.KILL_SWITCH_ENGAGED, "emergency kill switch is engaged")
    if limits is None:
        return reject(RiskReason.RISK_SETTINGS_NOT_CONFIGURED, "the user has not configured risk limits")
    if proposal.stop_loss_price is None:
        return reject(RiskReason.STOP_LOSS_REQUIRED, "entries must specify a stop-loss price")

    sizing_base = proposal.limit_price if proposal.order_type is OrderType.LIMIT else proposal.reference_price
    assert sizing_base is not None
    entry = costs.slipped_price(OrderSide.BUY, sizing_base)
    if proposal.order_type is OrderType.LIMIT:
        entry = min(entry, sizing_base)  # a limit BUY never fills above its limit
    if proposal.stop_loss_price >= entry:
        return reject(
            RiskReason.STOP_LOSS_NOT_BELOW_ENTRY, f"stop {proposal.stop_loss_price} is not below entry {entry}",
            sizing_price=entry,
        )
    if proposal.take_profit_price is not None and proposal.take_profit_price <= entry:
        return reject(
            RiskReason.TAKE_PROFIT_NOT_ABOVE_ENTRY,
            f"take-profit {proposal.take_profit_price} is not above entry {entry}",
            sizing_price=entry,
        )
    checks.append(RiskCheck("protective_levels", True, f"stop {proposal.stop_loss_price} < entry {entry}"))

    equity = snapshot.equity
    if equity is None or equity <= 0 or snapshot.gross_exposure is None:
        return reject(RiskReason.EQUITY_UNDETERMINED, snapshot.equity_detail, sizing_price=entry)

    if snapshot.peak_equity:
        dd = (snapshot.peak_equity - equity) / snapshot.peak_equity * HUNDRED
        if dd >= limits.max_drawdown_pct:
            return reject(
                RiskReason.MAX_DRAWDOWN_BREACHED,
                f"drawdown {dd:.4f}% >= limit {limits.max_drawdown_pct}%",
                sizing_price=entry,
            )
        checks.append(RiskCheck("max_drawdown", True, f"drawdown {dd:.4f}% < {limits.max_drawdown_pct}%"))
    if snapshot.day_start_equity:
        dl = (snapshot.day_start_equity - equity) / snapshot.day_start_equity * HUNDRED
        if dl >= limits.max_daily_loss_pct:
            return reject(
                RiskReason.DAILY_LOSS_LIMIT_BREACHED,
                f"daily loss {dl:.4f}% >= limit {limits.max_daily_loss_pct}%",
                sizing_price=entry,
            )
        checks.append(RiskCheck("daily_loss", True, f"daily loss {dl:.4f}% < {limits.max_daily_loss_pct}%"))

    # Hybrid rule: the strategy may only tighten the user's per-trade risk limit.
    effective_pct = limits.max_risk_per_trade_pct
    if proposal.strategy_max_risk_pct is not None:
        if proposal.strategy_max_risk_pct > limits.max_risk_per_trade_pct:
            checks.append(
                RiskCheck(
                    "strategy_risk_capped_by_user",
                    True,
                    f"strategy limit {proposal.strategy_max_risk_pct}% exceeds user hard limit "
                    f"{limits.max_risk_per_trade_pct}%; user limit applies",
                )
            )
        effective_pct = min(effective_pct, proposal.strategy_max_risk_pct)

    per_share_risk = entry - proposal.stop_loss_price
    per_share_cash = costs.buy_cost_per_share_upper(entry)
    position_value = snapshot.position_value or ZERO

    caps: list[RiskCheck] = [
        RiskCheck(
            "max_risk_per_trade",
            True,
            f"{effective_pct}% of equity {equity} / per-share risk {per_share_risk}",
            _floor(equity * effective_pct / HUNDRED / per_share_risk),
        ),
        RiskCheck(
            "max_position_allocation",
            True,
            f"{limits.max_position_allocation_pct}% of equity minus held {position_value} "
            f"and committed {snapshot.open_buy_commitment}",
            _floor(
                (equity * limits.max_position_allocation_pct / HUNDRED - position_value - snapshot.open_buy_commitment)
                / per_share_cash
            ),
        ),
        RiskCheck(
            "max_portfolio_exposure",
            True,
            f"{limits.max_portfolio_exposure_pct}% of equity minus exposure {snapshot.gross_exposure}",
            _floor((equity * limits.max_portfolio_exposure_pct / HUNDRED - snapshot.gross_exposure) / per_share_cash),
        ),
        RiskCheck(
            "available_cash",
            True,
            f"available {snapshot.available_cash}, worst-case fees included",
            costs.max_buy_quantity(entry, snapshot.available_cash),
        ),
    ]
    if limits.max_position_quantity is not None:
        caps.append(
            RiskCheck(
                "max_position_quantity",
                True,
                f"{limits.max_position_quantity} minus held {snapshot.position_quantity} "
                f"and open buys {snapshot.open_buy_quantity}",
                max(0, limits.max_position_quantity - snapshot.position_quantity - snapshot.open_buy_quantity),
            )
        )

    approved = proposal.quantity
    for cap in caps:
        assert cap.cap is not None
        cap.passed = proposal.quantity <= cap.cap
        approved = min(approved, cap.cap)
    if proposal.lot_size:
        approved -= approved % proposal.lot_size
    checks.extend(caps)
    binding = [c.name for c in caps if not c.passed]

    def result(outcome: RiskOutcome, qty: int, reasons: list[str]) -> RiskResult:
        return RiskResult(
            outcome,
            qty,
            sizing_price=entry,
            risk_amount=(per_share_risk * qty) if qty else None,
            effective_risk_pct=effective_pct,
            risk_limit_amount=(equity * effective_pct / HUNDRED).quantize(Decimal("0.000001"), rounding=ROUND_FLOOR),
            checks=checks,
            reasons=reasons,
        )

    if approved <= 0:
        return result(
            RiskOutcome.REJECTED, 0, [f"{RiskReason.SIZE_EXCEEDS_LIMITS.value}: no compliant size ({', '.join(binding)})"]
        )
    if approved < proposal.quantity:
        if not limits.resize_allowed:
            return result(
                RiskOutcome.REJECTED,
                0,
                [
                    f"{RiskReason.RESIZE_NOT_ALLOWED.value}: proposed {proposal.quantity} exceeds limits "
                    f"({', '.join(binding)}); compliant size {approved}"
                ],
            )
        return result(
            RiskOutcome.RESIZED, approved, [f"resized from {proposal.quantity} to {approved} ({', '.join(binding)})"]
        )
    return result(RiskOutcome.APPROVED, approved, [])
