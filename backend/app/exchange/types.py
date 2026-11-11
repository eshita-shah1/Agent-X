"""Order request types shared by the order pipeline, the risk engine and the exchange."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.db.enums import OrderSide, OrderSource, OrderType, Product


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else str(v.normalize())


@dataclass(frozen=True)
class OrderIntent:
    """A proposed order, before risk sizing. Strategies/agent/API produce these; they never
    touch balances or positions directly."""

    client_order_id: str
    instrument_id: int
    side: OrderSide
    order_type: OrderType
    quantity: int
    source: OrderSource
    limit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    strategy_id: str | None = None
    # Strategy-specific risk rule: the strategy's own maximum risk per trade (% of equity).
    # The effective limit is min(user limit, strategy limit); user limits always win.
    strategy_max_risk_pct: Decimal | None = None
    # When the decision was made (tz-aware). Defaults to the acceptance time for manual orders; strategies (Phase 3/5)
    # must pass the time of the bar their signal was computed from. Never later than acceptance.
    signal_at: datetime | None = None
    # Product whose cost rates apply. Only DELIVERY is executed today.
    product: Product = Product.DELIVERY

    def fingerprint(self) -> str:
        """Stable hash of the request, so an idempotent retry can be told apart from a
        different order reusing the same client_order_id."""
        payload = {
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "source": self.source.value,
            "limit_price": _dec(self.limit_price),
            "stop_loss_price": _dec(self.stop_loss_price),
            "take_profit_price": _dec(self.take_profit_price),
            "strategy_id": self.strategy_id,
            "strategy_max_risk_pct": _dec(self.strategy_max_risk_pct),
            "signal_at": self.signal_at.isoformat() if self.signal_at else None,
            "product": self.product.value,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
