"""Turn a submitted backtest request (plain JSON values) into the engine's immutable BacktestRequest.

The same function rebuilds the request inside the background job from the JSON stored on the run, and the job
checks that the rebuilt request has the stored config fingerprint, so what runs is exactly what was accepted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.backtest import BacktestRefused, BacktestRequest, CapitalSource, CostModel, RefusalCode
from app.strategies import configs
from app.strategies.base import StrategyConfig

BAD = RefusalCode.INVALID_CONFIGURATION


def build_config(strategy_id: str, params: dict[str, Any]) -> StrategyConfig:
    """A strategy configuration from its approved defaults plus explicit overrides (shared with the agent: decision
    D13). Unknown or mistyped parameters are refused as INVALID_CONFIGURATION; nothing is coerced silently."""
    try:
        return configs.build_config(strategy_id, params)
    except configs.ConfigurationError as exc:
        raise BacktestRefused(BAD, str(exc)) from None


def build_policy(mode: str, candidates: list[dict[str, Any]]):
    try:
        return configs.build_policy(mode, candidates)
    except configs.ConfigurationError as exc:
        raise BacktestRefused(BAD, str(exc)) from None


def build_request(submitted: dict[str, Any], capital: Decimal, capital_source: CapitalSource) -> BacktestRequest:
    """`submitted` is the JSON form of the API body (dates as ISO strings, decimals as strings)."""
    costs = submitted.get("cost_model") or {}
    try:
        cost_model = CostModel(**{k: Decimal(str(v)) for k, v in costs.items()})
    except (TypeError, ValueError) as exc:
        raise BacktestRefused(BAD, f"cost_model: {exc}") from None
    return BacktestRequest(
        instrument_ids=tuple(submitted["instrument_ids"]),
        start=date.fromisoformat(submitted["start"]),
        end=date.fromisoformat(submitted["end"]),
        policy=build_policy(submitted["mode"], submitted["candidates"]),
        capital=capital,
        capital_source=capital_source,
        cost_model=cost_model,
        warmup_sessions=submitted["warmup_sessions"],
    )
