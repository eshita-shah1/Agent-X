"""Canonical serialization and SHA-256 fingerprints.

Canonical form: JSON with sorted keys, no whitespace, ASCII only. Decimals are written in plain notation without
trailing zeros (1.50 and 1.5 are the same value and the same text); floats use their shortest round-trip repr;
datetimes are UTC ISO-8601 with `Z`; enums use their value; tuples and lists keep their (already deterministic)
order; mappings are ordered by key. Nothing depends on hash seeds, dictionary insertion order or the clock.

  * config_fingerprint: everything the user chose plus the engine/fill/sizing model versions.
  * data_fingerprint: every bar used (warm-up and evaluation, with its source reference), the corporate actions
    supplied, and the calendar sessions spanned.
  * result_fingerprint: every result output: data quality, selection decisions, trades, events, equity points,
    benchmark (with validity) and the summary, for every instrument.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from app.backtest.data import PreparedInstrument
from app.backtest.model import ENGINE_VERSION, FILL_MODEL, PRICE_BASIS, SIZING_MODEL, BacktestRequest, InstrumentResult
from app.marketdata.calendar import TradingCalendar
from app.strategies.registry import STRATEGIES
from app.strategies.selection import PinnedPolicy


def canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, enum.Enum):
        return canonical(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal {value}")
        text = format(value.normalize(), "f")
        return "0" if text in ("-0", "0") else text
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime in fingerprint input")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (tuple, list)):
        return [canonical(v) for v in value]
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def digest(value: Any) -> str:
    text = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def config_document(request: BacktestRequest) -> dict[str, Any]:
    policy = request.policy
    return {
        "engine_version": ENGINE_VERSION,
        "fill_model": FILL_MODEL,
        "sizing_model": SIZING_MODEL,
        "price_basis": PRICE_BASIS,
        "timeframe": request.timeframe,
        "instrument_ids": sorted((str(i) for i in request.instrument_ids)),
        "start": request.start,
        "end": request.end,
        "capital": request.capital,
        "cost_model": request.cost_model,
        "warmup_sessions": request.warmup_sessions,
        "policy": {
            "mode": policy.mode,
            "regime": policy.regime,
            "candidates": [
                {
                    "config_id": c.config_id,
                    "strategy_id": c.strategy_id,
                    "strategy_version": STRATEGIES[c.strategy_id].version,
                    "config_type": type(c.config).__name__,
                    "params": c.config,
                    "priority": None if isinstance(policy, PinnedPolicy) else c.priority,
                }
                for c in request.candidates
            ],
        },
    }


def config_fingerprint(request: BacktestRequest) -> str:
    return digest(config_document(request))


def data_fingerprint(prepared: tuple[PreparedInstrument, ...], calendar: TradingCalendar) -> str:
    doc = []
    for p in sorted(prepared, key=lambda p: str(p.instrument_id)):
        bars = p.bars
        sessions = calendar.sessions_between(bars[0].session_date, bars[-1].session_date)
        doc.append(
            {
                "instrument_id": str(p.instrument_id),
                "bars": [[b.start_ts, b.session_date, b.open, b.high, b.low, b.close, b.volume, b.source_ref] for b in bars],
                "corporate_actions": sorted((canonical(a) for a in p.corporate_actions), key=lambda a: json.dumps(a, sort_keys=True)),
                "calendar": {"source": calendar.source, "sessions": [[s.day, s.open_ts, s.close_ts, s.kind] for s in sessions]},
            }
        )
    return digest(doc)


def result_fingerprint(instruments: tuple[InstrumentResult, ...]) -> str:
    return digest(sorted((canonical(r) for r in instruments), key=lambda r: str(r["instrument_id"])))
