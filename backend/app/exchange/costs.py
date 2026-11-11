"""Simulated execution costs for Indian equities: a component fee model plus slippage.

Structure (every rate is configuration; **no value is built in**):

    brokerage   = min(brokerage_bps x turnover, brokerage_max_per_order - brokerage already charged on the order)
    stt         = turnover x stt_{buy|sell}_bps          (securities transaction tax, side-specific)
    txn_charge  = turnover x txn_charge_bps              (exchange transaction charges)
    sebi_fee    = turnover x sebi_fee_bps                (SEBI turnover fee)
    stamp_duty  = turnover x stamp_duty_buy_bps          (BUY side only)
    gst         = (brokerage + txn_charge + sebi_fee) x gst_pct / 100

The model is shared: backtesting and the Buy-and-Hold benchmark (Phase 6) must use this module so that
paper trading, backtests and benchmarks apply identical assumptions. The rates depend on the product
(intraday vs delivery), the broker and the date, which is why they are configuration, not code. If any rate
is unset the exchange rejects orders with COSTS_NOT_CONFIGURED instead of assuming zero cost. Zero is
allowed only when set explicitly.

Slippage: adverse, symmetric, in basis points of the reference price.

Rounding policy (documented, deterministic, always against the trader):
  * executed prices are quantized to 6 dp, BUY rounded up and SELL rounded down;
  * each fee component is quantized to 6 dp, rounded up.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.core.config import Settings
from app.db.enums import OrderSide

PRICE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.000001")
BPS_DIVISOR = Decimal(10000)
_COMPONENTS = 6
# Upper bound of the rounding added by ceiling-quantizing each component of one fill.
_ROUNDING_ALLOWANCE = MONEY_QUANTUM * _COMPONENTS


def _up(v: Decimal) -> Decimal:
    return v.quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)


def _floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR)) if x > 0 else 0


@dataclass(frozen=True)
class FeeBreakdown:
    brokerage: Decimal
    stt: Decimal
    txn_charge: Decimal
    sebi_fee: Decimal
    stamp_duty: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.txn_charge + self.sebi_fee + self.stamp_duty + self.gst

    def as_dict(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items()} | {"total": str(self.total)}


_SETTING_FIELDS = {
    "brokerage_bps": "exchange_brokerage_bps",
    "brokerage_max_per_order": "exchange_brokerage_max_per_order",
    "stt_buy_bps": "exchange_stt_buy_bps",
    "stt_sell_bps": "exchange_stt_sell_bps",
    "txn_charge_bps": "exchange_txn_charge_bps",
    "sebi_fee_bps": "exchange_sebi_fee_bps",
    "stamp_duty_buy_bps": "exchange_stamp_duty_buy_bps",
    "gst_pct": "exchange_gst_pct",
    "slippage_bps": "exchange_slippage_bps",
}


@dataclass(frozen=True)
class CostModel:
    brokerage_bps: Decimal
    brokerage_max_per_order: Decimal
    stt_buy_bps: Decimal
    stt_sell_bps: Decimal
    txn_charge_bps: Decimal
    sebi_fee_bps: Decimal
    stamp_duty_buy_bps: Decimal
    gst_pct: Decimal
    slippage_bps: Decimal
    max_volume_participation: Decimal | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "CostModel | None":
        values = {field: getattr(settings, name) for field, name in _SETTING_FIELDS.items()}
        if any(v is None for v in values.values()):
            return None
        return cls(**values, max_volume_participation=settings.exchange_max_volume_participation)

    @staticmethod
    def missing_settings(settings: Settings) -> list[str]:
        return [name.upper() for name in _SETTING_FIELDS.values() if getattr(settings, name) is None]

    # ---- prices -------------------------------------------------------------------------------

    def slipped_price(self, side: OrderSide, reference: Decimal) -> Decimal:
        """Adverse slippage: a BUY pays more, a SELL receives less."""
        factor = self.slippage_bps / BPS_DIVISOR
        if side is OrderSide.BUY:
            return (reference * (1 + factor)).quantize(PRICE_QUANTUM, rounding=ROUND_CEILING)
        return (reference * (1 - factor)).quantize(PRICE_QUANTUM, rounding=ROUND_FLOOR)

    # ---- fees -------------------------------------------------------------------------------

    def fees(self, side: OrderSide, turnover: Decimal, brokerage_already_charged: Decimal = Decimal(0)) -> FeeBreakdown:
        """Exact fees for one fill. `brokerage_already_charged` is the brokerage earlier fills of the same order
        paid, so the per-order brokerage cap holds across partial fills."""
        cap_left = max(Decimal(0), self.brokerage_max_per_order - brokerage_already_charged)
        brokerage = _up(min(turnover * self.brokerage_bps / BPS_DIVISOR, cap_left))
        stt_bps = self.stt_buy_bps if side is OrderSide.BUY else self.stt_sell_bps
        stt = _up(turnover * stt_bps / BPS_DIVISOR)
        txn = _up(turnover * self.txn_charge_bps / BPS_DIVISOR)
        sebi = _up(turnover * self.sebi_fee_bps / BPS_DIVISOR)
        stamp = _up(turnover * self.stamp_duty_buy_bps / BPS_DIVISOR) if side is OrderSide.BUY else Decimal(0)
        gst = _up((brokerage + txn + sebi) * self.gst_pct / 100)
        return FeeBreakdown(brokerage, stt, txn, sebi, stamp, gst)

    def fee(self, side: OrderSide, turnover: Decimal, brokerage_already_charged: Decimal = Decimal(0)) -> Decimal:
        return self.fees(side, turnover, brokerage_already_charged).total

    def _non_brokerage_rate(self, side: OrderSide) -> Decimal:
        """Fee rate (fraction of turnover) of every component except brokerage, including GST on charges."""
        g = 1 + self.gst_pct / 100
        stt = self.stt_buy_bps if side is OrderSide.BUY else self.stt_sell_bps
        stamp = self.stamp_duty_buy_bps if side is OrderSide.BUY else Decimal(0)
        return (stt + stamp + (self.txn_charge_bps + self.sebi_fee_bps) * g) / BPS_DIVISOR

    def buy_cost_per_share_upper(self, price: Decimal) -> Decimal:
        """Upper bound of the cash one share costs at `price`, all fees included (brokerage at its rate)."""
        g = 1 + self.gst_pct / 100
        return price * (1 + self._non_brokerage_rate(OrderSide.BUY) + self.brokerage_bps / BPS_DIVISOR * g)

    def max_buy_quantity(self, price: Decimal, cash: Decimal) -> int:
        """Largest quantity whose worst-case cost (turnover + every fee, incl. rounding) at `price` fits `cash`.

        need(q) = q*p*(1 + r) + min(q*p*b, cap)*(1 + g) + rounding. Both `q*p*b` and `cap` bound the brokerage
        term, so each yields a safe quantity; the larger of the two is still safe.
        """
        if price <= 0:
            return 0
        g = 1 + self.gst_pct / 100
        r = self._non_brokerage_rate(OrderSide.BUY)
        b = self.brokerage_bps / BPS_DIVISOR
        usable = cash - _ROUNDING_ALLOWANCE
        by_rate = _floor_int(usable / (price * (1 + r + b * g)))
        by_cap = _floor_int((usable - self.brokerage_max_per_order * g) / (price * (1 + r)))
        return max(by_rate, by_cap)

    def snapshot(self) -> dict:
        return {k: (str(v) if v is not None else None) for k, v in asdict(self).items()}
