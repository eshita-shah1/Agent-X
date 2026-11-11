"""The strategy catalogue: stable identifiers, versions, families and approved initial defaults.

Strategy definitions live in code (approved plan): user configurations will reference (strategy_id, version).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any

from app.marketdata.calendar import TradingCalendar
from app.strategies.base import Family, Strategy, StrategyConfig
from app.strategies.ema_crossover import EMACrossover
from app.strategies.orb import OpeningRangeBreakout
from app.strategies.params import DEFAULTS_NOTE, DEFAULTS_STATUS
from app.strategies.rsi_bollinger import RSIBollingerReversion
from app.strategies.supertrend import SupertrendFlip
from app.strategies.vwap import VWAPCross


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_type: type[Strategy]

    @property
    def strategy_id(self) -> str:
        return self.strategy_type.strategy_id

    @property
    def version(self) -> str:
        return self.strategy_type.version

    @property
    def family(self) -> Family:
        return self.strategy_type.family

    @property
    def config_type(self) -> type[StrategyConfig]:
        return self.strategy_type.config_type

    @property
    def description(self) -> str:
        """The strategy module's docstring: its exact entry and exit rules."""
        return (sys.modules[self.strategy_type.__module__].__doc__ or "").strip()

    def default_config(self) -> StrategyConfig:
        return self.config_type()

    def defaults(self) -> dict[str, Any]:
        config = self.default_config()
        return {f.name: getattr(config, f.name) for f in fields(config)}

    def create(self, config: StrategyConfig | None = None, calendar: TradingCalendar | None = None) -> Strategy:
        return self.strategy_type(config, calendar)


STRATEGIES = MappingProxyType(
    {
        d.strategy_id: d
        for d in (
            StrategyDefinition(OpeningRangeBreakout),
            StrategyDefinition(VWAPCross),
            StrategyDefinition(EMACrossover),
            StrategyDefinition(RSIBollingerReversion),
            StrategyDefinition(SupertrendFlip),
        )
    }
)


def get_definition(strategy_id: str) -> StrategyDefinition:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {sorted(STRATEGIES)}") from None


__all__ = ["DEFAULTS_NOTE", "DEFAULTS_STATUS", "STRATEGIES", "StrategyDefinition", "get_definition"]
