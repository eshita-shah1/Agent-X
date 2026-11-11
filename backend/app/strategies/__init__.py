"""Deterministic, long-only intraday strategies (Phase 3, Step 2).

Pure Python on top of `app.indicators`: no database, API, execution or LLM code. A strategy turns finalized
5-minute bars into Signals; filling, stops/targets, sizing and selection belong to later steps.
"""

from app.strategies.base import FLAT, Action, Family, PositionContext, ProtectiveLevels, Reason, Signal, Strategy, StrategyConfig
from app.strategies.ema_crossover import EMACrossover
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.params import (
    DEFAULTS_NOTE,
    DEFAULTS_STATUS,
    EMACrossoverConfig,
    ORBConfig,
    RSIBollingerConfig,
    SupertrendConfig,
    VWAPConfig,
)
from app.strategies.registry import STRATEGIES, StrategyDefinition, get_definition
from app.strategies.rsi_bollinger import RSIBollingerReversion
from app.strategies.supertrend import SupertrendFlip
from app.strategies.vwap import VWAPCross

__all__ = [
    "DEFAULTS_NOTE",
    "DEFAULTS_STATUS",
    "EMACrossover",
    "EMACrossoverConfig",
    "FLAT",
    "Action",
    "Family",
    "ORBConfig",
    "OpeningRangeBreakout",
    "PositionContext",
    "ProtectiveLevels",
    "RSIBollingerConfig",
    "RSIBollingerReversion",
    "Reason",
    "STRATEGIES",
    "Signal",
    "Strategy",
    "StrategyConfig",
    "StrategyDefinition",
    "SupertrendConfig",
    "SupertrendFlip",
    "VWAPConfig",
    "VWAPCross",
    "get_definition",
]
