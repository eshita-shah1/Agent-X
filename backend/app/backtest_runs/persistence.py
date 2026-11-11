"""Engine results -> database rows, and stored rows -> JSON-ready API values.

Decimals are stored in unconstrained NUMERIC columns (exact) or, inside JSONB, as canonical strings. Selection
explanations are stored exactly as `SelectionDecision.as_dict()` produced them.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.backtest import BacktestResult, canonical


@dataclass
class ResultRows:
    instruments: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)


TRADE_FIELDS = (
    "seq", "session_date", "config_id", "strategy_id", "strategy_version", "entry_signal_bar_start", "entry_signal_reason",
    "entry_reference_price", "entry_bar_start", "entry_price", "quantity", "stop_level", "target_level", "entry_fees",
    "exit_signal_bar_start", "exit_signal_reason", "exit_reference_price", "exit_bar_start", "exit_timing", "exit_price",
    "exit_reason", "exit_fees", "gross_pnl", "net_pnl",
)
EQUITY_FIELDS = ("bar_start", "session_date", "cash", "quantity", "close", "equity", "benchmark_equity")


def result_rows(run_id: uuid.UUID, user_id: uuid.UUID, result: BacktestResult) -> ResultRows:
    rows = ResultRows()
    for inst in result.instruments:
        owner = {"run_id": run_id, "user_id": user_id, "instrument_id": inst.instrument_id}
        rows.instruments.append(
            {
                **owner, "data_quality": canonical(inst.data_quality), "benchmark_status": inst.benchmark.status,
                "benchmark": canonical(inst.benchmark), "summary": canonical(inst.summary),
                "final_equity": inst.summary.final_equity, "benchmark_final_value": inst.benchmark.final_value,
            }
        )
        for d in inst.selection_decisions:
            selected = d["selected"]
            rows.decisions.append(
                {
                    **owner, "session_date": date.fromisoformat(d["session_date"]), "status": d["status"],
                    "selected_config_id": None if selected is None else selected["config_id"], "decision": d,
                }
            )
        for t in inst.trades:
            rows.trades.append({**owner, **{f: getattr(t, f) for f in TRADE_FIELDS}})
        for e in inst.events:
            rows.events.append(
                {
                    **owner, "seq": e.seq, "bar_start": e.bar_start, "session_date": e.session_date, "event_type": e.event_type,
                    "config_id": e.config_id, "detail": canonical(e.detail),
                }
            )
        for p in inst.equity:
            rows.equity.append({**owner, **{f: getattr(p, f) for f in EQUITY_FIELDS}})
    return rows


def run_summary(result: BacktestResult) -> dict[str, Any]:
    """A compact, descriptive overview stored on the run. Advisory only (decision D15)."""
    return canonical(
        {
            "cost_basis": result.cost_basis,
            "price_basis": result.price_basis,
            "per_instrument_not_portfolio": True,
            "instruments": [
                {
                    "instrument_id": i.instrument_id,
                    "effective_start": i.data_quality.effective_start,
                    "effective_end": i.data_quality.effective_end,
                    "coverage_pct": i.data_quality.coverage_pct,
                    "final_equity": i.summary.final_equity,
                    "return_pct": i.summary.return_pct,
                    "trades": i.summary.trades,
                    "max_drawdown_pct": i.summary.max_drawdown_pct,
                    "benchmark_status": i.benchmark.status,
                    "benchmark_return_pct": i.benchmark.return_pct,
                }
                for i in result.instruments
            ],
        }
    )


def plain(value: Any) -> Any:
    """A stored column value as JSON: Decimal -> exact string, datetime -> UTC `Z`, enum -> value."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def row_out(row: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {n: plain(getattr(row, n)) for n in names}
