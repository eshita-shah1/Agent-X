"""Simulated execution costs: fees and slippage.

The model is deliberately generic (fixed fee per order plus side-specific basis points of
turnover, and symmetric adverse slippage in basis points). No values are built in: they come
from configuration, and if any is missing `CostModel.from_settings` returns None and the
exchange refuses to execute rather than silently assuming zero cost.

Rounding policy (documented, deterministic, always against the trader):
  * executed prices are quantized to 6 dp, BUY rounded up and SELL rounded down;
  * fees are quantized to 6 dp, rounded up.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.core.config import Settings
from app.db.enums import OrderSide

PRICE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.000001")
BPS_DIVISOR = Decimal(10000)


@dataclass(frozen=True)
class CostModel:
    fee_fixed_per_order: Decimal
    fee_buy_bps: Decimal
    fee_sell_bps: Decimal
    slippage_bps: Decimal
    max_volume_participation: Decimal | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "CostModel | None":
        values = (
            settings.exchange_fee_fixed_per_order,
            settings.exchange_fee_buy_bps,
            settings.exchange_fee_sell_bps,
            settings.exchange_slippage_bps,
        )
        if any(v is None for v in values):
            return None
        return cls(*values, max_volume_participation=settings.exchange_max_volume_participation)  # type: ignore[arg-type]

    def fee_bps(self, side: OrderSide) -> Decimal:
        return self.fee_buy_bps if side is OrderSide.BUY else self.fee_sell_bps

    def slipped_price(self, side: OrderSide, reference: Decimal) -> Decimal:
        """Adverse slippage: a BUY pays more, a SELL receives less."""
        factor = self.slippage_bps / BPS_DIVISOR
        if side is OrderSide.BUY:
            return (reference * (1 + factor)).quantize(PRICE_QUANTUM, rounding=ROUND_CEILING)
        return (reference * (1 - factor)).quantize(PRICE_QUANTUM, rounding=ROUND_FLOOR)

    def fee(self, side: OrderSide, turnover: Decimal, include_fixed: bool) -> Decimal:
        variable = turnover * self.fee_bps(side) / BPS_DIVISOR
        total = variable + (self.fee_fixed_per_order if include_fixed else Decimal(0))
        return total.quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)

    def snapshot(self) -> dict:
        return {
            "fee_fixed_per_order": str(self.fee_fixed_per_order),
            "fee_buy_bps": str(self.fee_buy_bps),
            "fee_sell_bps": str(self.fee_sell_bps),
            "slippage_bps": str(self.slippage_bps),
            "max_volume_participation": (
                str(self.max_volume_participation) if self.max_volume_participation is not None else None
            ),
        }
