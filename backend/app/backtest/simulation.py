"""Run-level entry points: validate a request against its data, then simulate every instrument independently.

`prepare_backtest` performs every refusal check without simulating anything (Step 4B will call it before queueing a
job, to answer HTTP 422). `run_backtest` prepares, simulates each instrument with the full capital, and fingerprints
the configuration, the data and every result output. Pure: it reads nothing and writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.backtest.data import PreparedInstrument, prepare_instrument
from app.backtest.engine import InstrumentEngine, StrategyFactory, default_strategy_factory
from app.backtest.fingerprint import config_fingerprint, data_fingerprint, result_fingerprint
from app.backtest.model import (
    ENGINE_VERSION,
    FILL_MODEL,
    PRICE_BASIS,
    SIZING_MODEL,
    BacktestRefused,
    BacktestRequest,
    BacktestResult,
    InstrumentData,
    RefusalCode,
)
from app.marketdata.calendar import TradingCalendar

DISCLOSURES = (
    "COST_BASIS: results are labelled GROSS_OF_COSTS when every cost and slippage setting is zero (the default), "
    "otherwise NET_OF_MODELLED_COSTS; modelled costs are user assumptions, not statutory rates.",
    "PRICE_BASIS: prices are Yahoo Finance provider-native values labelled UNVERIFIED_PROVIDER_NATIVE; whether they "
    "are as-traded or adjusted has not been verified.",
    "PER_INSTRUMENT: each instrument is simulated independently with the full capital; results are not a portfolio "
    "and must not be summed.",
    "FILL_MODEL: NEXT_BAR_OPEN_V1 is a backtest-only approximation (signal at a bar's close, entry/exit at the next "
    "bar's open; stops and targets from the bar's open/high/low). It is not the Mock Exchange and not a broker.",
    "STRUCTURAL_LOOKAHEAD: to expire entries and apply the session square-off fallback the engine looks at the "
    "timestamp of the next bar (whether it exists and is contiguous), never at its prices.",
    "YAHOO_LIMITS: about 60 days of 5-minute history; bars may be missing or later restated; no authoritative NSE "
    "holiday calendar is integrated; missing bars are never filled or interpolated.",
    "CORPORATE_ACTIONS: a split/bonus ex-date inside the evaluation period refuses the backtest (D11); warm-up "
    "history before such an ex-date is dropped; dividends are excluded; recorded actions come from the provider and "
    "may be incomplete.",
    "STATISTICS: about 60 days of data is not statistically sufficient; results are not evidence of future "
    "performance.",
    "ADVISORY_ONLY: backtest results never influence strategy selection and never activate a strategy.",
    "BENCHMARK: buy-and-hold holds overnight, unlike the intraday strategy; see each benchmark's validity.",
)


def prepare_backtest(request: BacktestRequest, data: Mapping[object, InstrumentData], calendar: TradingCalendar | None = None) -> tuple[PreparedInstrument, ...]:
    calendar = calendar or TradingCalendar()
    if not isinstance(request, BacktestRequest):
        raise BacktestRefused(RefusalCode.INVALID_CONFIGURATION, "request must be a BacktestRequest")
    prepared = []
    for iid in request.instrument_ids:
        item = data.get(iid)
        if item is None or not item.bars:
            raise BacktestRefused(RefusalCode.NO_DATA, "no bars supplied", iid)
        if item.instrument_id != iid:
            raise BacktestRefused(RefusalCode.INVALID_DATA, f"data is labelled {item.instrument_id!r}", iid)
        prepared.append(prepare_instrument(request, item, calendar))
    return tuple(prepared)


def run_backtest(
    request: BacktestRequest, data: Mapping[object, InstrumentData], calendar: TradingCalendar | None = None,
    strategy_factory: StrategyFactory = default_strategy_factory,
) -> BacktestResult:
    calendar = calendar or TradingCalendar()
    prepared = prepare_backtest(request, data, calendar)  # every refusal happens before any simulation
    instruments = tuple(InstrumentEngine(request, p, calendar, strategy_factory).run() for p in prepared)
    return BacktestResult(
        engine_version=ENGINE_VERSION,
        fill_model=FILL_MODEL,
        sizing_model=SIZING_MODEL,
        price_basis=PRICE_BASIS,
        cost_basis=request.cost_model.cost_basis,
        capital=request.capital,
        capital_source=request.capital_source,
        requested_start=request.start,
        requested_end=request.end,
        config_fingerprint=config_fingerprint(request),
        data_fingerprint=data_fingerprint(prepared, calendar),
        result_fingerprint=result_fingerprint(instruments),
        instruments=instruments,
        disclosures=DISCLOSURES,
    )
