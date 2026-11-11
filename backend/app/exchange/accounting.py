"""Pure position accounting (long-only), separated from persistence so it is unit-testable.

Average entry price is volume-weighted and excludes fees (fees are tracked separately), kept at
10 dp with banker's rounding. Realized P&L is gross of fees, quantized to 6 dp.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

AVG_QUANTUM = Decimal("0.0000000001")
MONEY_QUANTUM = Decimal("0.000001")


class AccountingError(ValueError):
    pass


@dataclass(frozen=True)
class PositionState:
    quantity: int
    avg_entry_price: Decimal | None
    realized_pnl: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise AccountingError("long-only: quantity cannot be negative")
        if (self.quantity == 0) != (self.avg_entry_price is None):
            raise AccountingError("average entry price must be set iff the position is open")


def apply_buy(state: PositionState, quantity: int, price: Decimal) -> PositionState:
    if quantity <= 0 or price <= 0:
        raise AccountingError("buy quantity and price must be positive")
    held_cost = (state.avg_entry_price or Decimal(0)) * state.quantity
    new_qty = state.quantity + quantity
    avg = ((held_cost + price * quantity) / new_qty).quantize(AVG_QUANTUM, rounding=ROUND_HALF_EVEN)
    return PositionState(new_qty, avg, state.realized_pnl)


def apply_sell(state: PositionState, quantity: int, price: Decimal) -> tuple[PositionState, Decimal]:
    """Returns the new state and the realized P&L (gross of fees) of this sale."""
    if quantity <= 0 or price <= 0:
        raise AccountingError("sell quantity and price must be positive")
    if quantity > state.quantity:
        raise AccountingError("long-only: cannot sell more than the held quantity")
    assert state.avg_entry_price is not None
    realized = ((price - state.avg_entry_price) * quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    remaining = state.quantity - quantity
    return (
        PositionState(remaining, state.avg_entry_price if remaining else None, state.realized_pnl + realized),
        realized,
    )


def unrealized_pnl(state: PositionState, mark: Decimal) -> Decimal:
    if state.quantity == 0:
        return Decimal(0)
    assert state.avg_entry_price is not None
    return ((mark - state.avg_entry_price) * state.quantity).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
