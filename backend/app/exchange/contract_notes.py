"""Contract-note validation for the cost model (B2): recompute a broker contract note's charges from a cost schedule
and compare them, component by component, with the amounts printed on the note.

This is how a production cost schedule is validated: a schedule built from the broker's official tariff is only
accepted once it reproduces real contract notes exactly. It uses the same `CostModel` as paper trading, backtesting
and the benchmark, applied in the same way (per-order brokerage cap, DP once per scrip per sell day, cumulative
rounding by stage). Pure: no database, no clock.

Contract-note file (JSON):
    {
      "contract_note_id": "...", "broker": "...", "trade_date": "YYYY-MM-DD", "product": "DELIVERY",
      "synthetic": true | false,          # true = a constructed example, NOT evidence of broker compatibility
      "source": "...",                    # e.g. "Zerodha contract note, redacted"
      "schedule": "relative/path/to/schedule.json",
      "trades": [{"order_ref": "...", "instrument": "...", "side": "BUY|SELL", "quantity": 10, "price": "100.05"}],
      "expected": {"BROKERAGE": "...", "STT": "...", "TXN_CHARGE": "...", "SEBI_FEE": "...", "IPFT": "...",
                   "STAMP_DUTY": "...", "DP_CHARGE": "...", "GST": "...", "total": "..."}
    }
Trades are applied in file order (the order they appear on the note). Prices are executed prices: no slippage.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.db.enums import OrderSide, Product
from app.exchange.costs import (
    MONEY_CHARGES,
    MONEY_QUANTUM,
    ChargeType,
    CostSchedule,
    FeeContext,
    RoundingStage,
)

ZERO = Decimal(0)


@dataclass(frozen=True)
class ComponentCheck:
    charge: str
    expected: Decimal | None
    computed: Decimal

    @property
    def ok(self) -> bool:
        return self.expected is None or self.expected == self.computed


@dataclass(frozen=True)
class ContractNoteReport:
    contract_note_id: str
    synthetic: bool
    schedule_version: str
    schedule_sha256: str
    components: list[ComponentCheck]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.components)

    def as_dict(self) -> dict:
        return {
            "contract_note_id": self.contract_note_id,
            "synthetic": self.synthetic,
            "evidence": "SYNTHETIC formula check only" if self.synthetic else "real contract note",
            "schedule_version": self.schedule_version,
            "schedule_sha256": self.schedule_sha256,
            "ok": self.ok,
            "components": [
                {"charge": c.charge, "expected": str(c.expected) if c.expected is not None else None,
                 "computed": str(c.computed), "ok": c.ok}
                for c in self.components
            ],
        }


def evaluate_contract_note(schedule: CostSchedule, note: dict) -> ContractNoteReport:
    product = Product(note.get("product", "DELIVERY"))
    trade_date = date.fromisoformat(note["trade_date"])
    costs = schedule.resolve(product, trade_date)
    totals = {c: ZERO for c in MONEY_CHARGES}
    # per-charge rounding groups: (raw, charged) running sums
    order_sums: dict[str, dict[ChargeType, list[Decimal]]] = {}
    session_sums: dict[ChargeType, list[Decimal]] = {}
    instrument_sums: dict[str, dict[ChargeType, list[Decimal]]] = {}
    order_brokerage_raw: dict[str, Decimal] = {}
    order_seen: set[str] = set()
    sold_today: set[str] = set()
    for t in note["trades"]:
        side = OrderSide(t["side"])
        order_ref, instrument = t["order_ref"], t["instrument"]
        turnover = (Decimal(t["price"]) * int(t["quantity"])).quantize(MONEY_QUANTUM)
        prior = {}
        for charge, rule in costs.rounding.items():
            if rule.stage is RoundingStage.ORDER:
                group = order_sums.setdefault(order_ref, {})
            elif rule.stage is RoundingStage.SESSION:
                group = session_sums
            elif rule.stage is RoundingStage.SESSION_INSTRUMENT:
                group = instrument_sums.setdefault(instrument, {})
            else:
                continue
            raw, charged = group.get(charge, [ZERO, ZERO])
            prior[charge] = (raw, charged)
        ctx = FeeContext(
            first_fill_of_order=order_ref not in order_seen,
            brokerage_raw_on_order=order_brokerage_raw.get(order_ref, ZERO),
            dp_already_charged=side is OrderSide.SELL and instrument in sold_today,
            prior=prior,
        )
        fees = costs.fees(side, turnover, ctx)
        for charge, comp in fees.components.items():
            totals[charge] += comp.charged
            for group in (
                order_sums.setdefault(order_ref, {}), session_sums, instrument_sums.setdefault(instrument, {})
            ):
                cur = group.setdefault(charge, [ZERO, ZERO])
                cur[0] += comp.raw
                cur[1] += comp.charged
        order_seen.add(order_ref)
        order_brokerage_raw[order_ref] = order_brokerage_raw.get(order_ref, ZERO) + (
            fees.components[ChargeType.BROKERAGE].raw if ChargeType.BROKERAGE in fees.components else ZERO
        )
        if side is OrderSide.SELL:
            sold_today.add(instrument)
    expected = note.get("expected", {})
    checks = [
        ComponentCheck(c.value, Decimal(expected[c.value]) if c.value in expected else None, totals[c])
        for c in MONEY_CHARGES
    ]
    total = sum(totals.values(), ZERO)
    checks.append(ComponentCheck("total", Decimal(expected["total"]) if "total" in expected else None, total))
    return ContractNoteReport(
        note.get("contract_note_id", "?"), bool(note.get("synthetic", False)), schedule.version, schedule.sha256, checks
    )
