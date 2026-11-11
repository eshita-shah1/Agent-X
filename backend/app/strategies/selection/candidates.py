"""Selection candidates, regime eligibility and warm-up requirements.

A candidate is a strategy configuration with an explicit priority (1 = highest). It carries no trading outcome
of any kind: selection is decided by regime, warm-up and priority only (decision D15).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.strategies.base import Family, StrategyConfig
from app.strategies.registry import STRATEGIES
from app.strategies.selection.regime import Regime

ELIGIBLE_FAMILIES: dict[Regime, frozenset[Family]] = {
    Regime.TREND: frozenset({Family.TREND, Family.OPENING_BREAKOUT}),
    Regime.RANGE: frozenset({Family.MEAN_REVERSION}),
    Regime.UNDETERMINED: frozenset(),
}


@dataclass(frozen=True)
class Candidate:
    """`config_id` identifies the user's configuration; `priority` orders candidates in regime mode."""

    config_id: str
    strategy_id: str
    config: Any
    priority: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config_id, str) or not self.config_id.strip():
            raise ValueError("config_id must be a non-empty string")
        if not isinstance(self.strategy_id, str):
            raise ValueError("strategy_id must be a string")
        if self.priority is not None and (isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 1):
            raise ValueError(f"priority must be an integer >= 1, got {self.priority!r}")


def configuration_problem(candidate: Candidate) -> str | None:
    """Why the candidate cannot be used, or None. Never raises: an unusable candidate is reported, not hidden."""
    definition = STRATEGIES.get(candidate.strategy_id)
    if definition is None:
        return f"unknown strategy {candidate.strategy_id!r}"
    if type(candidate.config) is not definition.config_type:
        return f"{candidate.strategy_id} needs a {definition.config_type.__name__}, got {type(candidate.config).__name__}"
    return None


def required_bars(strategy_id: str, config: StrategyConfig) -> int | None:
    """Bars (across sessions) after which every indicator the strategy needs is defined: from that bar on the
    strategy no longer reports INDICATOR_NOT_READY. None for ORB, whose readiness is the session's opening range."""
    if strategy_id == "ORB":
        return None
    if strategy_id == "VWAP":
        return config.atr_period + 1
    if strategy_id == "EMA_CROSSOVER":
        return max(config.slow_period + 1, config.atr_period + 1)  # the cross needs the previous slow value
    if strategy_id == "RSI_BOLLINGER":
        return max(config.rsi_period + 1, config.bollinger_period, config.atr_period + 1)
    if strategy_id == "SUPERTREND":
        return config.atr_period + 1
    raise ValueError(f"no warm-up rule for strategy {strategy_id!r}")


class CandidateOutcome(str, enum.Enum):
    SELECTED = "SELECTED"
    NOT_SELECTED_LOWER_PRIORITY = "NOT_SELECTED_LOWER_PRIORITY"
    REJECTED_INVALID_CONFIGURATION = "REJECTED_INVALID_CONFIGURATION"
    REJECTED_REGIME_INELIGIBLE = "REJECTED_REGIME_INELIGIBLE"
    REJECTED_WARMUP_NOT_SATISFIED = "REJECTED_WARMUP_NOT_SATISFIED"


@dataclass(frozen=True)
class CandidateEvaluation:
    config_id: str
    strategy_id: str
    strategy_version: str | None  # None when the strategy is unknown
    family: str | None
    priority: int | None
    configuration_valid: bool
    regime_eligible: bool | None  # None when not assessed (invalid configuration); informational in pinned mode
    warmup_requirement: str | None  # e.g. "22 bars" or "opening range COMPLETE by the checkpoint"
    warmup_satisfied: bool | None
    outcome: CandidateOutcome
    detail: str
