"""Cost and slippage model, and whole-share sizing. All money is Decimal.

Defaults are zero (approved): a run with the default model is labelled GROSS_OF_COSTS. No statutory Indian rates
are built in; any non-zero value is a user-supplied assumption and labels the run NET_OF_MODELLED_COSTS.

  * slippage_bps moves every simulated price against the trader: buys higher, sells lower.
  * fee_per_order is charged once per order with a non-zero quantity; fee_bps_per_side applies to the notional of
    every order; sell_side_bps applies to sell orders only (e.g. a sell-side transaction tax).
  * Fees are rounded to the paisa (0.01, half-up) per order. Prices and cash are otherwise never rounded.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

PAISA = Decimal("0.01")
BPS = Decimal(10000)


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class CostBasis(str, enum.Enum):
    GROSS_OF_COSTS = "GROSS_OF_COSTS"
    NET_OF_MODELLED_COSTS = "NET_OF_MODELLED_COSTS"


def to_decimal(name: str, value: Any) -> Decimal:
    """A finite Decimal from a Decimal, int or numeric string. Floats are rejected: money must not pass through
    binary floating point."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError(f"{name} must be a Decimal, int or numeric string, got {type(value).__name__}")
    try:
        d = Decimal(value)
    except Exception:  # noqa: BLE001 - decimal raises InvalidOperation
        raise ValueError(f"{name} is not a number: {value!r}") from None
    if not d.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return d


def level_to_decimal(value: float) -> Decimal:
    """A strategy's float level or distance (stop/target) as the Decimal of its shortest round-trip representation.
    No rounding and no tick-size normalization (Step 4A review, decision 4)."""
    return Decimal(repr(float(value)))


@dataclass(frozen=True)
class CostModel:
    fee_per_order: Decimal = Decimal(0)
    fee_bps_per_side: Decimal = Decimal(0)
    sell_side_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("fee_per_order", "fee_bps_per_side", "sell_side_bps", "slippage_bps"):
            d = to_decimal(name, getattr(self, name))
            if d < 0:
                raise ValueError(f"{name} must be >= 0, got {d}")
            object.__setattr__(self, name, d)
        if self.slippage_bps >= BPS:
            raise ValueError("slippage_bps must be below 10000 (100%)")

    @property
    def cost_basis(self) -> CostBasis:
        zero = not any((self.fee_per_order, self.fee_bps_per_side, self.sell_side_bps, self.slippage_bps))
        return CostBasis.GROSS_OF_COSTS if zero else CostBasis.NET_OF_MODELLED_COSTS

    def price(self, side: Side, raw: Decimal) -> Decimal:
        """The simulated price after adverse slippage."""
        factor = self.slippage_bps / BPS
        return raw * (1 + factor) if side is Side.BUY else raw * (1 - factor)

    def fees(self, side: Side, notional: Decimal) -> Decimal:
        if notional == 0:
            return Decimal(0)
        bps = self.fee_bps_per_side + (self.sell_side_bps if side is Side.SELL else 0)
        return (self.fee_per_order + notional * bps / BPS).quantize(PAISA, rounding=ROUND_HALF_UP)


def whole_shares(cash: Decimal, price: Decimal, costs: CostModel) -> int:
    """The largest whole number of shares whose cost plus buy fees fits in `cash` (allocation 1.0, no leverage)."""
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")
    if cash <= 0:
        return 0
    rate = 1 + (costs.fee_bps_per_side / BPS)
    qty = max(int((cash - costs.fee_per_order) / (price * rate)), 0)  # close estimate, then exact adjustment

    def fits(q: int) -> bool:
        notional = q * price
        return notional + costs.fees(Side.BUY, notional) <= cash

    while qty > 0 and not fits(qty):
        qty -= 1
    while fits(qty + 1):
        qty += 1
    return qty
