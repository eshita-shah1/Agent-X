"""Turn a submitted backtest request (plain JSON values) into the engine's immutable BacktestRequest.

The same function rebuilds the request inside the background job from the JSON stored on the run, and the job
checks that the rebuilt request has the stored config fingerprint, so what runs is exactly what was accepted.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import date, time
from decimal import Decimal
from typing import Any

from app.backtest import BacktestRefused, BacktestRequest, CapitalSource, CostModel, RefusalCode
from app.strategies.base import StrategyConfig
from app.strategies.registry import STRATEGIES
from app.strategies.selection import Candidate, PinnedPolicy, RegimePriorityPolicy

BAD = RefusalCode.INVALID_CONFIGURATION


def build_config(strategy_id: str, params: dict[str, Any]) -> StrategyConfig:
    """A strategy configuration from its approved defaults plus explicit overrides. Unknown or mistyped parameters
    are refused; nothing is coerced silently (times are "HH:MM" or "HH:MM:SS" strings)."""
    definition = STRATEGIES.get(strategy_id)
    if definition is None:
        raise BacktestRefused(BAD, f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}")
    if not isinstance(params, dict):
        raise BacktestRefused(BAD, f"{strategy_id}: params must be an object")
    known = {f.name: f for f in fields(definition.config_type)}
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise BacktestRefused(BAD, f"{strategy_id}: unknown parameter(s) {unknown}; known: {sorted(known)}")
    kwargs = {}
    for name, value in params.items():
        default = known[name].default if known[name].default is not MISSING else None
        if isinstance(default, time):
            if not isinstance(value, str):
                raise BacktestRefused(BAD, f"{strategy_id}.{name} must be a time string such as '14:45'")
            try:
                value = time.fromisoformat(value)
            except ValueError:
                raise BacktestRefused(BAD, f"{strategy_id}.{name}: invalid time {value!r}") from None
        elif isinstance(default, bool) or isinstance(value, bool):
            raise BacktestRefused(BAD, f"{strategy_id}.{name}: booleans are not accepted")
        elif isinstance(default, int):
            if not isinstance(value, int):
                raise BacktestRefused(BAD, f"{strategy_id}.{name} must be an integer")
        elif isinstance(default, float):
            if not isinstance(value, (int, float)):
                raise BacktestRefused(BAD, f"{strategy_id}.{name} must be a number")
            value = float(value)
        kwargs[name] = value
    try:
        return definition.config_type(**kwargs)
    except (ValueError, TypeError) as exc:
        raise BacktestRefused(BAD, f"{strategy_id}: {exc}") from None


def build_policy(mode: str, candidates: list[dict[str, Any]]):
    if not candidates:
        raise BacktestRefused(BAD, "at least one candidate is required")
    built = []
    for c in candidates:
        try:
            built.append(Candidate(c["config_id"], c["strategy_id"], build_config(c["strategy_id"], c.get("params") or {}), c.get("priority")))
        except BacktestRefused:
            raise
        except (KeyError, ValueError) as exc:
            raise BacktestRefused(BAD, f"invalid candidate: {exc}") from None
    try:
        if mode == "PINNED":
            if len(built) != 1:
                raise BacktestRefused(BAD, "PINNED mode takes exactly one candidate")
            if built[0].priority is not None:
                raise BacktestRefused(BAD, "a pinned candidate has no priority")
            return PinnedPolicy(built[0])
        if mode == "REGIME_PRIORITY":
            return RegimePriorityPolicy(tuple(built))
    except ValueError as exc:
        if isinstance(exc, BacktestRefused):
            raise
        raise BacktestRefused(BAD, str(exc)) from None
    raise BacktestRefused(BAD, f"unknown selection mode {mode!r}")


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
