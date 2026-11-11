"""Phase 5: autonomous agent orchestration (paper trading only).

The agent orchestrates existing services and owns no trading logic of its own:
  MarketDataService (archived, finalized bars) -> the approved selector and strategies (deterministic replay)
  -> agent decisions -> OrderService.submit -> Phase 4 risk engine -> Mock Exchange.
It never writes orders, fills, positions or ledger entries, never reads backtest results, never schedules itself
(`python -m app.agent.cli cycle` is run by an external scheduler) and contains no LLM or broker code.
"""
