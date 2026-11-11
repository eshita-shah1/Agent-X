"""Deterministic, pure backtesting engine (Phase 3, Step 4A).

NEXT_BAR_OPEN_V1 fills, long-only whole-share sizing, protective stops/targets, configurable costs (default zero,
GROSS_OF_COSTS), Decimal accounting, per-bar equity, an independent buy-and-hold benchmark and SHA-256 fingerprints.
Each instrument is simulated on its own. No database, API, account, Mock Exchange, broker or LLM code: results are
advisory and never drive strategy selection (decision D15).
"""

from app.backtest.costs import CostBasis, CostModel, Side, whole_shares
from app.backtest.data import PreparedInstrument, prepare_instrument
from app.backtest.engine import InstrumentEngine, default_strategy_factory
from app.backtest.fingerprint import canonical, config_fingerprint, data_fingerprint, digest, result_fingerprint
from app.backtest.model import (
    DEFAULT_WARMUP_SESSIONS,
    ENGINE_VERSION,
    FILL_MODEL,
    PRICE_BASIS,
    SIZING_MODEL,
    BacktestBar,
    BacktestRefused,
    BacktestRequest,
    BacktestResult,
    BenchmarkResult,
    BenchmarkStatus,
    CapitalSource,
    CorporateActionRecord,
    DataQuality,
    EquityPoint,
    Event,
    EventType,
    ExitReason,
    ExitTiming,
    InstrumentData,
    InstrumentResult,
    InstrumentSummary,
    RefusalCode,
    Trade,
    WarmupSource,
)
from app.backtest.simulation import DISCLOSURES, prepare_backtest, run_backtest

__all__ = [
    "DEFAULT_WARMUP_SESSIONS",
    "DISCLOSURES",
    "ENGINE_VERSION",
    "FILL_MODEL",
    "PRICE_BASIS",
    "SIZING_MODEL",
    "BacktestBar",
    "BacktestRefused",
    "BacktestRequest",
    "BacktestResult",
    "BenchmarkResult",
    "BenchmarkStatus",
    "CapitalSource",
    "CorporateActionRecord",
    "CostBasis",
    "CostModel",
    "DataQuality",
    "EquityPoint",
    "Event",
    "EventType",
    "ExitReason",
    "ExitTiming",
    "InstrumentData",
    "InstrumentEngine",
    "InstrumentResult",
    "InstrumentSummary",
    "PreparedInstrument",
    "RefusalCode",
    "Side",
    "Trade",
    "WarmupSource",
    "canonical",
    "config_fingerprint",
    "data_fingerprint",
    "default_strategy_factory",
    "digest",
    "prepare_backtest",
    "prepare_instrument",
    "result_fingerprint",
    "run_backtest",
    "whole_shares",
]
