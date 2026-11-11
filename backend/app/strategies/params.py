"""Immutable, validated strategy configurations with the approved initial defaults (Phase 3 decision D7).

The defaults are APPROVED INITIAL DEFAULTS: they were not optimized against any data and are not evidence of
profitability. Changing a configuration means creating a new one (decision D14); these objects are frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from app.indicators.core import check_number, check_period
from app.strategies.base import StrategyConfig, check_session_time

DEFAULTS_STATUS = "APPROVED_INITIAL_DEFAULTS"
DEFAULTS_NOTE = "Approved initial defaults (decision D7); not empirically optimized and not evidence of profitability."


def check_positive(name: str, value: Any) -> float:
    v = check_number(name, value)
    if v <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return v


@dataclass(frozen=True)
class ORBConfig(StrategyConfig):
    entry_cutoff: time = time(11, 30)  # ORB's own, earlier cutoff
    range_minutes: int = 15
    target_multiple: float = 1.5  # target distance = target_multiple * opening-range height

    def __post_init__(self) -> None:
        super().__post_init__()
        check_period("range_minutes", self.range_minutes)
        if self.range_minutes % 5:
            raise ValueError(f"range_minutes must be a multiple of 5, got {self.range_minutes!r}")
        check_positive("target_multiple", self.target_multiple)


@dataclass(frozen=True)
class VWAPConfig(StrategyConfig):
    eligible_from: time = time(9, 30)  # no entry signal on a bar ending before this time
    atr_period: int = 14
    stop_atr_multiple: float = 1.5

    def __post_init__(self) -> None:
        super().__post_init__()
        check_session_time("eligible_from", self.eligible_from)
        if self.eligible_from > self.entry_cutoff:
            raise ValueError("eligible_from must not be later than entry_cutoff")
        check_period("atr_period", self.atr_period)
        check_positive("stop_atr_multiple", self.stop_atr_multiple)


@dataclass(frozen=True)
class EMACrossoverConfig(StrategyConfig):
    fast_period: int = 9
    slow_period: int = 21
    atr_period: int = 14
    stop_atr_multiple: float = 2.0

    def __post_init__(self) -> None:
        super().__post_init__()
        check_period("fast_period", self.fast_period)
        check_period("slow_period", self.slow_period)
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        check_period("atr_period", self.atr_period)
        check_positive("stop_atr_multiple", self.stop_atr_multiple)


@dataclass(frozen=True)
class RSIBollingerConfig(StrategyConfig):
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0  # validated but unused while long-only: reserved for the deferred short side
    bollinger_period: int = 20
    bollinger_k: float = 2.0
    atr_period: int = 14
    stop_atr_multiple: float = 1.5

    def __post_init__(self) -> None:
        super().__post_init__()
        check_period("rsi_period", self.rsi_period)
        oversold, overbought = check_number("oversold", self.oversold), check_number("overbought", self.overbought)
        if not 0 < oversold < overbought < 100:
            raise ValueError("thresholds must satisfy 0 < oversold < overbought < 100")
        check_period("bollinger_period", self.bollinger_period)
        check_positive("bollinger_k", self.bollinger_k)
        check_period("atr_period", self.atr_period)
        check_positive("stop_atr_multiple", self.stop_atr_multiple)


@dataclass(frozen=True)
class SupertrendConfig(StrategyConfig):
    atr_period: int = 10
    multiplier: float = 3.0

    def __post_init__(self) -> None:
        super().__post_init__()
        check_period("atr_period", self.atr_period)
        check_positive("multiplier", self.multiplier)
