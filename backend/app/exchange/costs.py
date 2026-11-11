"""Indian cash-equity transaction costs: an effective-dated, product-aware component model plus slippage.

This is the single cost implementation for paper trading, historical backtesting and the Buy-and-Hold benchmark
(Phase 6 must import it; no engine may re-implement these formulas). It is pure and deterministic: no database,
no clock, Decimal arithmetic only.

Rates come from an operator-maintained **cost schedule** (JSON file, `EXCHANGE_COST_SCHEDULE_PATH`); **no rate is
built into the code**. Each entry carries a rate id, product (DELIVERY / INTRADAY), charge type, side, calculation
basis, value, effective-from / effective-to dates (from inclusive, to exclusive) and a source reference. The
schedule has a version and its SHA-256 is recorded on every fill, together with the exact rate ids used, so every
fill's cost assumptions are reproducible. A missing rate for the transaction's product, side and date is an explicit
error; zero is accepted only when written explicitly, and an all-zero rate set is refused.

Charge types and calculation bases:
    BROKERAGE   TURNOVER_BPS | TURNOVER_BPS_CAPPED_PER_ORDER (cap across the order's partial fills) | FLAT_PER_ORDER
    STT, TXN_CHARGE, SEBI_FEE, IPFT, STAMP_DUTY   TURNOVER_BPS (stamp duty: BUY side only)
    DP_CHARGE   FLAT_PER_SCRIP_SELL_DAY: once per instrument per simulated session date, SELL only, DELIVERY only
    GST         PCT_OF_CHARGES on the configured `applies_to` components (e.g. brokerage, transaction charge, SEBI fee,
                IPFT and, where the broker applies it, DP charge)
    SLIPPAGE    PRICE_BPS, adverse (BUY pays more, SELL receives less); prices rounded against the trader to 6 dp

Calculation order (per fill):
    1. turnover = quantity x executed price
    2. raw base components (brokerage, STT, transaction charge, SEBI fee, IPFT, stamp duty, DP charge)
    3. each base component is charged under its rounding rule
    4. GST raw = GST% x sum of the *charged* components in `applies_to`; then charged under its own rounding rule
    5. total = sum of charged components

Rounding: every charge type has its own rule (stage, quantum, mode); nothing assumes one rounding for all fees.
Stages: FILL (each fill on its own), ORDER (cumulative over the order's fills), SESSION (cumulative over the
account's fills on the simulated session date, i.e. contract-note level), SESSION_INSTRUMENT (same, per instrument).
Cumulative rounding charges `round(prior_raw + raw) - prior_charged`, so the group's charged total always equals the
rounded total of its raw amounts and is never negative.
"""

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.db.enums import OrderSide, Product

PRICE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.000001")
BPS_DIVISOR = Decimal(10000)
ZERO = Decimal(0)


class ChargeType(str, enum.Enum):
    BROKERAGE = "BROKERAGE"
    STT = "STT"
    TXN_CHARGE = "TXN_CHARGE"
    SEBI_FEE = "SEBI_FEE"
    IPFT = "IPFT"
    STAMP_DUTY = "STAMP_DUTY"
    DP_CHARGE = "DP_CHARGE"
    GST = "GST"
    SLIPPAGE = "SLIPPAGE"


BASE_CHARGES = (
    ChargeType.BROKERAGE, ChargeType.STT, ChargeType.TXN_CHARGE, ChargeType.SEBI_FEE, ChargeType.IPFT,
    ChargeType.STAMP_DUTY, ChargeType.DP_CHARGE,
)
MONEY_CHARGES = BASE_CHARGES + (ChargeType.GST,)


class RateSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    BOTH = "BOTH"


class RateBasis(str, enum.Enum):
    TURNOVER_BPS = "TURNOVER_BPS"
    TURNOVER_BPS_CAPPED_PER_ORDER = "TURNOVER_BPS_CAPPED_PER_ORDER"
    FLAT_PER_ORDER = "FLAT_PER_ORDER"
    FLAT_PER_SCRIP_SELL_DAY = "FLAT_PER_SCRIP_SELL_DAY"
    PCT_OF_CHARGES = "PCT_OF_CHARGES"
    PRICE_BPS = "PRICE_BPS"


class RoundingStage(str, enum.Enum):
    FILL = "FILL"
    ORDER = "ORDER"
    SESSION = "SESSION"
    SESSION_INSTRUMENT = "SESSION_INSTRUMENT"


class RoundingMode(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"
    HALF_UP = "HALF_UP"
    HALF_EVEN = "HALF_EVEN"


_ROUNDING = {
    RoundingMode.UP: ROUND_CEILING,
    RoundingMode.DOWN: ROUND_DOWN,
    RoundingMode.HALF_UP: ROUND_HALF_UP,
    RoundingMode.HALF_EVEN: ROUND_HALF_EVEN,
}

_ALLOWED_BASES = {
    ChargeType.BROKERAGE: {RateBasis.TURNOVER_BPS, RateBasis.TURNOVER_BPS_CAPPED_PER_ORDER, RateBasis.FLAT_PER_ORDER},
    ChargeType.STT: {RateBasis.TURNOVER_BPS},
    ChargeType.TXN_CHARGE: {RateBasis.TURNOVER_BPS},
    ChargeType.SEBI_FEE: {RateBasis.TURNOVER_BPS},
    ChargeType.IPFT: {RateBasis.TURNOVER_BPS},
    ChargeType.STAMP_DUTY: {RateBasis.TURNOVER_BPS},
    ChargeType.DP_CHARGE: {RateBasis.FLAT_PER_SCRIP_SELL_DAY},
    ChargeType.GST: {RateBasis.PCT_OF_CHARGES},
    ChargeType.SLIPPAGE: {RateBasis.PRICE_BPS},
}


def _required_sides(product: Product, charge: ChargeType) -> tuple[OrderSide, ...]:
    """Sides for which a rate must exist (an explicit zero is a valid rate)."""
    if charge is ChargeType.STAMP_DUTY:
        return (OrderSide.BUY,)
    if charge is ChargeType.DP_CHARGE:
        return (OrderSide.SELL,) if product is Product.DELIVERY else ()
    return (OrderSide.BUY, OrderSide.SELL)


class CostScheduleError(ValueError):
    """The schedule is invalid, or has no applicable rate for a product, side and date."""


# ---- schedule file model ---------------------------------------------------------------------------------


class RateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_id: str = Field(min_length=1, max_length=64)
    product: Product
    charge_type: ChargeType
    side: RateSide
    basis: RateBasis
    value: Decimal = Field(ge=0)  # bps, flat INR, or percent (GST), per `basis`
    cap: Decimal | None = Field(default=None, ge=0)  # TURNOVER_BPS_CAPPED_PER_ORDER only
    applies_to: tuple[ChargeType, ...] = ()  # GST only
    effective_from: date
    effective_to: date | None = None  # exclusive
    source: str = Field(min_length=1, max_length=500)  # authority / circular / tariff reference
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "RateEntry":
        if self.basis not in _ALLOWED_BASES[self.charge_type]:
            raise ValueError(f"{self.rate_id}: basis {self.basis.value} is not valid for {self.charge_type.value}")
        if (self.basis is RateBasis.TURNOVER_BPS_CAPPED_PER_ORDER) != (self.cap is not None):
            raise ValueError(f"{self.rate_id}: `cap` is required for, and only for, TURNOVER_BPS_CAPPED_PER_ORDER")
        if self.charge_type is ChargeType.GST:
            if not self.applies_to:
                raise ValueError(f"{self.rate_id}: GST requires a non-empty `applies_to`")
            if any(c not in BASE_CHARGES for c in self.applies_to):
                raise ValueError(f"{self.rate_id}: GST `applies_to` may only list base charges")
        elif self.applies_to:
            raise ValueError(f"{self.rate_id}: `applies_to` is only valid for GST")
        if self.charge_type is ChargeType.STAMP_DUTY and self.side is not RateSide.BUY:
            raise ValueError(f"{self.rate_id}: stamp duty applies to the BUY side only")
        if self.charge_type is ChargeType.DP_CHARGE:
            if self.side is not RateSide.SELL:
                raise ValueError(f"{self.rate_id}: DP charges apply to the SELL side only")
            if self.product is not Product.DELIVERY:
                raise ValueError(f"{self.rate_id}: DP charges apply to DELIVERY only")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(f"{self.rate_id}: effective_to must be after effective_from")
        return self

    def active_on(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d < self.effective_to)

    def covers(self, side: OrderSide) -> bool:
        return self.side is RateSide.BOTH or self.side.value == side.value

    def as_dict(self) -> dict:
        return {
            "rate_id": self.rate_id, "basis": self.basis.value, "value": str(self.value),
            "cap": str(self.cap) if self.cap is not None else None,
            "applies_to": [c.value for c in self.applies_to], "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None, "source": self.source,
        }


class RoundingRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    charge_type: ChargeType
    stage: RoundingStage
    quantum: Decimal = Field(gt=0)  # e.g. 0.01 (paise) or 1 (rupee)
    mode: RoundingMode

    @field_validator("charge_type")
    @classmethod
    def _money_charge(cls, v: ChargeType) -> ChargeType:
        if v is ChargeType.SLIPPAGE:
            raise ValueError("slippage changes the price (6 dp, against the trader); it has no money rounding rule")
        return v

    def apply(self, v: Decimal) -> Decimal:
        return (v / self.quantum).to_integral_value(rounding=_ROUNDING[self.mode]) * self.quantum

    def as_dict(self) -> dict:
        return {"stage": self.stage.value, "quantum": str(self.quantum), "mode": self.mode.value}


class CostScheduleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_version: str = Field(min_length=1, max_length=64)
    currency: str = Field(pattern="^INR$")
    description: str | None = None
    rounding: list[RoundingRule]
    rates: list[RateEntry]

    @model_validator(mode="after")
    def _schedule_consistent(self) -> "CostScheduleFile":
        ids = [r.rate_id for r in self.rates]
        if len(ids) != len(set(ids)):
            raise ValueError("rate_id values must be unique")
        types = [r.charge_type for r in self.rounding]
        if len(types) != len(set(types)):
            raise ValueError("at most one rounding rule per charge type")
        missing = [c.value for c in MONEY_CHARGES if c not in types]
        if missing:
            raise ValueError(f"a rounding rule is required for every money charge; missing: {missing}")
        # No two entries may apply to the same product, charge, side and day.
        for i, a in enumerate(self.rates):
            for b in self.rates[i + 1:]:
                if a.product is not b.product or a.charge_type is not b.charge_type:
                    continue
                sides_overlap = a.side is RateSide.BOTH or b.side is RateSide.BOTH or a.side is b.side
                a_end, b_end = a.effective_to or date.max, b.effective_to or date.max
                if sides_overlap and a.effective_from < b_end and b.effective_from < a_end:
                    raise ValueError(f"rates {a.rate_id} and {b.rate_id} overlap in effective dates")
        return self


# ---- resolved model for one product and date ---------------------------------------------------------------


@dataclass(frozen=True)
class FeeContext:
    """What the cost model needs to know about earlier fills (supplied by the caller; the model stays pure)."""

    first_fill_of_order: bool = True
    brokerage_raw_on_order: Decimal = ZERO  # for the per-order cap
    dp_already_charged: bool = False  # a SELL of this instrument already booked on this simulated session date
    # Per charge type: (raw, charged) sums already booked in that charge type's rounding group.
    prior: dict[ChargeType, tuple[Decimal, Decimal]] = field(default_factory=dict)


@dataclass(frozen=True)
class FeeComponent:
    raw: Decimal
    charged: Decimal
    rate_id: str | None


@dataclass(frozen=True)
class FeeBreakdown:
    components: dict[ChargeType, FeeComponent]

    @property
    def total(self) -> Decimal:
        return sum((c.charged for c in self.components.values()), ZERO)

    def charged(self, charge: ChargeType) -> Decimal:
        c = self.components.get(charge)
        return c.charged if c else ZERO

    def as_dict(self) -> dict:
        return {
            "components": {
                k.value: {"raw": str(v.raw), "charged": str(v.charged), "rate_id": v.rate_id}
                for k, v in self.components.items()
            },
            "total": str(self.total),
        }


def breakdown_component(breakdown: dict | None, charge: ChargeType) -> tuple[Decimal, Decimal]:
    """(raw, charged) of one component in a stored `fills.fee_breakdown`. Reads the pre-0007 flat format too, in
    which only charged amounts were stored (raw is then taken as equal to charged)."""
    if not breakdown:
        return ZERO, ZERO
    comps = breakdown.get("components")
    if isinstance(comps, dict):
        c = comps.get(charge.value)
        return (Decimal(c["raw"]), Decimal(c["charged"])) if c else (ZERO, ZERO)
    legacy = {
        ChargeType.BROKERAGE: "brokerage", ChargeType.STT: "stt", ChargeType.TXN_CHARGE: "txn_charge",
        ChargeType.SEBI_FEE: "sebi_fee", ChargeType.STAMP_DUTY: "stamp_duty", ChargeType.GST: "gst",
    }.get(charge)
    if legacy and legacy in breakdown:
        v = Decimal(breakdown[legacy])
        return v, v
    return ZERO, ZERO


@dataclass(frozen=True)
class CostModel:
    product: Product
    on_date: date
    rates: dict[tuple[ChargeType, OrderSide], RateEntry]
    rounding: dict[ChargeType, RoundingRule]
    schedule_version: str
    schedule_sha256: str
    max_volume_participation: Decimal | None = None

    def rate(self, charge: ChargeType, side: OrderSide) -> RateEntry | None:
        return self.rates.get((charge, side))

    def rounding_stage(self, charge: ChargeType) -> RoundingStage:
        return self.rounding[charge].stage

    def _value(self, charge: ChargeType, side: OrderSide) -> Decimal:
        r = self.rate(charge, side)
        return r.value if r else ZERO

    # ---- prices -------------------------------------------------------------------------------

    def slipped_price(self, side: OrderSide, reference: Decimal) -> Decimal:
        factor = self._value(ChargeType.SLIPPAGE, side) / BPS_DIVISOR
        if side is OrderSide.BUY:
            return (reference * (1 + factor)).quantize(PRICE_QUANTUM, rounding=ROUND_CEILING)
        return (reference * (1 - factor)).quantize(PRICE_QUANTUM, rounding=ROUND_FLOOR)

    # ---- fees -------------------------------------------------------------------------------

    def _raw_base(self, side: OrderSide, turnover: Decimal, ctx: FeeContext) -> dict[ChargeType, Decimal]:
        raw: dict[ChargeType, Decimal] = {}
        b = self.rate(ChargeType.BROKERAGE, side)
        if b is not None:
            if b.basis is RateBasis.FLAT_PER_ORDER:
                raw[ChargeType.BROKERAGE] = b.value if ctx.first_fill_of_order else ZERO
            elif b.basis is RateBasis.TURNOVER_BPS_CAPPED_PER_ORDER:
                assert b.cap is not None
                left = max(ZERO, b.cap - ctx.brokerage_raw_on_order)
                raw[ChargeType.BROKERAGE] = min(turnover * b.value / BPS_DIVISOR, left)
            else:
                raw[ChargeType.BROKERAGE] = turnover * b.value / BPS_DIVISOR
        for charge in (ChargeType.STT, ChargeType.TXN_CHARGE, ChargeType.SEBI_FEE, ChargeType.IPFT, ChargeType.STAMP_DUTY):
            r = self.rate(charge, side)
            if r is not None:
                raw[charge] = turnover * r.value / BPS_DIVISOR
        dp = self.rate(ChargeType.DP_CHARGE, side)
        if dp is not None:
            raw[ChargeType.DP_CHARGE] = ZERO if ctx.dp_already_charged else dp.value
        return raw

    def _charge(self, charge: ChargeType, raw: Decimal, ctx: FeeContext) -> Decimal:
        rule = self.rounding[charge]
        if rule.stage is RoundingStage.FILL:
            return rule.apply(raw)
        prior_raw, prior_charged = ctx.prior.get(charge, (ZERO, ZERO))
        return max(ZERO, rule.apply(prior_raw + raw) - prior_charged)

    def fees(self, side: OrderSide, turnover: Decimal, ctx: FeeContext | None = None) -> FeeBreakdown:
        ctx = ctx or FeeContext()
        comps: dict[ChargeType, FeeComponent] = {}
        for charge, raw in self._raw_base(side, turnover, ctx).items():
            r = self.rate(charge, side)
            comps[charge] = FeeComponent(raw, self._charge(charge, raw, ctx), r.rate_id if r else None)
        gst = self.rate(ChargeType.GST, side)
        if gst is not None:
            base = sum((comps[c].charged for c in gst.applies_to if c in comps), ZERO)
            raw = base * gst.value / 100
            comps[ChargeType.GST] = FeeComponent(raw, self._charge(ChargeType.GST, raw, ctx), gst.rate_id)
        return FeeBreakdown(comps)

    def fee(self, side: OrderSide, turnover: Decimal, ctx: FeeContext | None = None) -> Decimal:
        return self.fees(side, turnover, ctx).total

    # ---- conservative bounds (risk sizing and cash reservation) -----------------------------------------

    def _gst_factor(self, side: OrderSide, charge: ChargeType) -> Decimal:
        g = self.rate(ChargeType.GST, side)
        return 1 + g.value / 100 if g is not None and charge in g.applies_to else Decimal(1)

    def _rounding_allowance(self, side: OrderSide) -> Decimal:
        """Upper bound of what rounding can add to one fill: at most one quantum per component (cumulative rounding
        charges at most one quantum above the fill's raw amount), GST included."""
        g = self.rate(ChargeType.GST, side)
        gst_factor = 1 + (g.value / 100 if g is not None else ZERO)
        return sum((self.rounding[c].quantum for c in MONEY_CHARGES if c in self.rounding), ZERO) * gst_factor

    def _variable_rate(self, side: OrderSide, include_brokerage: bool) -> Decimal:
        rate = ZERO
        for charge in (ChargeType.STT, ChargeType.TXN_CHARGE, ChargeType.SEBI_FEE, ChargeType.IPFT, ChargeType.STAMP_DUTY):
            r = self.rate(charge, side)
            if r is not None:
                rate += r.value / BPS_DIVISOR * self._gst_factor(side, charge)
        b = self.rate(ChargeType.BROKERAGE, side)
        if include_brokerage and b is not None and b.basis is not RateBasis.FLAT_PER_ORDER:
            rate += b.value / BPS_DIVISOR * self._gst_factor(side, ChargeType.BROKERAGE)
        return rate

    def _fixed_upper(self, side: OrderSide) -> Decimal:
        b = self.rate(ChargeType.BROKERAGE, side)
        fixed = ZERO
        if b is not None and b.basis is RateBasis.FLAT_PER_ORDER:
            fixed += b.value * self._gst_factor(side, ChargeType.BROKERAGE)
        dp = self.rate(ChargeType.DP_CHARGE, side)
        if dp is not None:
            fixed += dp.value * self._gst_factor(side, ChargeType.DP_CHARGE)
        return fixed + self._rounding_allowance(side)

    def buy_cost_per_share_upper(self, price: Decimal) -> Decimal:
        """Upper bound of the variable cash cost of one share at `price` (fees included, fixed parts excluded)."""
        return price * (1 + self._variable_rate(OrderSide.BUY, include_brokerage=True))

    def buy_cost_upper(self, price: Decimal, quantity: int) -> Decimal:
        """Upper bound of the total cash a BUY of `quantity` at `price` can cost, every fee and rounding included.
        A capped brokerage is bounded both by its rate and by its cap; both bounds are valid, the smaller is used."""
        fixed = self._fixed_upper(OrderSide.BUY)
        by_rate = self.buy_cost_per_share_upper(price) * quantity + fixed
        b = self.rate(ChargeType.BROKERAGE, OrderSide.BUY)
        if b is not None and b.basis is RateBasis.TURNOVER_BPS_CAPPED_PER_ORDER:
            assert b.cap is not None
            by_cap = (
                price * quantity * (1 + self._variable_rate(OrderSide.BUY, False))
                + fixed + b.cap * self._gst_factor(OrderSide.BUY, ChargeType.BROKERAGE)
            )
            return min(by_rate, by_cap)
        return by_rate

    def max_buy_quantity(self, price: Decimal, cash: Decimal) -> int:
        """Largest quantity whose worst-case total cost at `price` fits `cash`."""
        if price <= 0:
            return 0
        fixed = self._fixed_upper(OrderSide.BUY)
        by_rate = _floor_int((cash - fixed) / (price * (1 + self._variable_rate(OrderSide.BUY, True))))
        b = self.rate(ChargeType.BROKERAGE, OrderSide.BUY)
        if b is not None and b.basis is RateBasis.TURNOVER_BPS_CAPPED_PER_ORDER:
            assert b.cap is not None
            capped_fixed = fixed + b.cap * self._gst_factor(OrderSide.BUY, ChargeType.BROKERAGE)
            by_cap = _floor_int((cash - capped_fixed) / (price * (1 + self._variable_rate(OrderSide.BUY, False))))
            return max(by_rate, by_cap)
        return by_rate

    def snapshot(self) -> dict:
        return {
            "schedule_version": self.schedule_version,
            "schedule_sha256": self.schedule_sha256,
            "product": self.product.value,
            "rate_date": self.on_date.isoformat(),
            "rates": {f"{c.value}:{s.value}": r.as_dict() for (c, s), r in sorted(self.rates.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value))},
            "rounding": {c.value: r.as_dict() for c, r in self.rounding.items()},
            "max_volume_participation": (
                str(self.max_volume_participation) if self.max_volume_participation is not None else None
            ),
        }


def _floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR)) if x > 0 else 0


# ---- the schedule ------------------------------------------------------------------------------------------


class CostSchedule:
    def __init__(self, file: CostScheduleFile, sha256: str):
        self.file = file
        self.sha256 = sha256

    @property
    def version(self) -> str:
        return self.file.schedule_version

    def problems(self, product: Product, on_date: date) -> list[str]:
        problems = []
        for charge in ChargeType:
            sides = (OrderSide.BUY, OrderSide.SELL) if charge in (ChargeType.GST, ChargeType.SLIPPAGE) else _required_sides(product, charge)
            for side in sides:
                if not any(r.product is product and r.charge_type is charge and r.covers(side) and r.active_on(on_date)
                           for r in self.file.rates):
                    problems.append(f"no {charge.value} rate for {product.value} {side.value} on {on_date.isoformat()}")
        if not problems:
            active = [r for r in self.file.rates if r.product is product and r.active_on(on_date)]
            if all(r.value == 0 and (r.cap or 0) == 0 for r in active):
                problems.append(f"every {product.value} rate on {on_date.isoformat()} is zero; zero costs cannot enable execution")
        return problems

    def resolve(self, product: Product, on_date: date, max_volume_participation: Decimal | None = None) -> CostModel:
        problems = self.problems(product, on_date)
        if problems:
            raise CostScheduleError("; ".join(problems))
        rates: dict[tuple[ChargeType, OrderSide], RateEntry] = {}
        for r in self.file.rates:
            if r.product is not product or not r.active_on(on_date):
                continue
            for side in (OrderSide.BUY, OrderSide.SELL):
                if r.covers(side):
                    rates[(r.charge_type, side)] = r
        return CostModel(
            product=product, on_date=on_date, rates=rates, rounding={r.charge_type: r for r in self.file.rounding},
            schedule_version=self.file.schedule_version, schedule_sha256=self.sha256,
            max_volume_participation=max_volume_participation,
        )


def parse_cost_schedule(content: bytes) -> CostSchedule:
    try:
        file = CostScheduleFile.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError) as e:
        raise CostScheduleError(f"invalid cost schedule: {e}") from None
    return CostSchedule(file, hashlib.sha256(content).hexdigest())


def load_cost_schedule(path: str | Path) -> CostSchedule:
    try:
        content = Path(path).read_bytes()
    except OSError as e:
        raise CostScheduleError(f"cannot read cost schedule {path}: {e}") from None
    return parse_cost_schedule(content)
