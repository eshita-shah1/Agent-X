"""Database side of backtesting (Phase 3, Step 4B): loading archived data, user-owned runs as background jobs,
and atomic, immutable persistence of the pure engine's results (`app.backtest`, which never touches a database)."""
