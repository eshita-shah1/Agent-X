"""Strategy configurations and selection policies from plain JSON values, and the corporate-action warm-up rule.

Shared by the backtest runner and the agent (approved Phase 5 decision D13), so both build exactly the same immutable
configuration from the same submitted values and drop the same warm-up sessions around a split or bonus. Nothing here
reads data, a clock or a database.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from datetime import date, time
from typing import Any, Iterable

from app.strategies.base import StrategyConfig
from app.strategies.registry import STRATEGIES
from app.strategies.selection import Candidate, PinnedPolicy, RegimePriorityPolicy

# Split and bonus ex-dates change the price scale of a series (approved Phase 3 decision D11).
SCALE_CHANGING_ACTION_TYPES = frozenset({"SPLIT", "BONUS"})


class ConfigurationError(ValueError):
    """The submitted strategy configuration or selection policy is not usable."""


def build_config(strategy_id: str, params: dict[str, Any]) -> StrategyConfig:
    """A strategy configuration from its approved defaults plus explicit overrides. Unknown or mistyped parameters
    are refused; nothing is coerced silently (times are "HH:MM" or "HH:MM:SS" strings)."""
    definition = STRATEGIES.get(strategy_id)
    if definition is None:
        raise ConfigurationError(f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}")
    if not isinstance(params, dict):
        raise ConfigurationError(f"{strategy_id}: params must be an object")
    known = {f.name: f for f in fields(definition.config_type)}
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise ConfigurationError(f"{strategy_id}: unknown parameter(s) {unknown}; known: {sorted(known)}")
    kwargs = {}
    for name, value in params.items():
        default = known[name].default if known[name].default is not MISSING else None
        if isinstance(default, time):
            if not isinstance(value, str):
                raise ConfigurationError(f"{strategy_id}.{name} must be a time string such as '14:45'")
            try:
                value = time.fromisoformat(value)
            except ValueError:
                raise ConfigurationError(f"{strategy_id}.{name}: invalid time {value!r}") from None
        elif isinstance(default, bool) or isinstance(value, bool):
            raise ConfigurationError(f"{strategy_id}.{name}: booleans are not accepted")
        elif isinstance(default, int):
            if not isinstance(value, int):
                raise ConfigurationError(f"{strategy_id}.{name} must be an integer")
        elif isinstance(default, float):
            if not isinstance(value, (int, float)):
                raise ConfigurationError(f"{strategy_id}.{name} must be a number")
            value = float(value)
        kwargs[name] = value
    try:
        return definition.config_type(**kwargs)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{strategy_id}: {exc}") from None


def build_policy(mode: str, candidates: list[dict[str, Any]]):
    if not candidates:
        raise ConfigurationError("at least one candidate is required")
    built = []
    for c in candidates:
        try:
            built.append(Candidate(c["config_id"], c["strategy_id"], build_config(c["strategy_id"], c.get("params") or {}), c.get("priority")))
        except ConfigurationError:
            raise
        except (KeyError, ValueError) as exc:
            raise ConfigurationError(f"invalid candidate: {exc}") from None
    try:
        if mode == "PINNED":
            if len(built) != 1:
                raise ConfigurationError("PINNED mode takes exactly one candidate")
            if built[0].priority is not None:
                raise ConfigurationError("a pinned candidate has no priority")
            return PinnedPolicy(built[0])
        if mode == "REGIME_PRIORITY":
            return RegimePriorityPolicy(tuple(built))
    except ValueError as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(str(exc)) from None
    raise ConfigurationError(f"unknown selection mode {mode!r}")


def lookback_days(warmup_sessions: int) -> int:
    """Calendar days loaded before `start` so that `warmup_sessions` prior sessions can be found (weekends and a
    few closures included). If fewer exist, the engine shifts the warm-up into the period and discloses it."""
    return 7 + 3 * warmup_sessions


def corporate_action_floor(actions: Iterable[tuple[date, str]], start: date) -> date | None:
    """The latest split/bonus ex-date at or before `start` (ex-date, action type pairs), or None. Bars of earlier
    sessions are on another price scale and must not feed indicators that carry into `start`."""
    return max((ex_date for ex_date, action_type in actions if action_type in SCALE_CHANGING_ACTION_TYPES and ex_date <= start), default=None)


def warmup_sessions_before(prior_sessions: list[date], floor: date | None, warmup_sessions: int) -> tuple[list[date], date | None]:
    """The last `warmup_sessions` sessions with data before the start, never earlier than the corporate-action floor.
    Returns (sessions used, the floor when it actually removed a session that would otherwise have been used)."""
    usable = [d for d in prior_sessions if floor is None or d >= floor]
    used = usable[-warmup_sessions:] if warmup_sessions else []
    truncated = floor if warmup_sessions and len(used) < min(warmup_sessions, len(prior_sessions)) else None
    return used, truncated
