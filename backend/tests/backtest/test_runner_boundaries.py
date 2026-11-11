"""The database-side runner and the API stay within Step 4B: no provider access, ingestion, scheduler, LLM, order or
exchange code, and the only write endpoint starts a run."""

import ast
import pathlib
import re

from app.api.v1.backtests import router

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
FILES = [*sorted((APP / "backtest_runs").glob("*.py")), APP / "api" / "v1" / "backtests.py", APP / "api" / "v1" / "schemas_backtest.py"]
BANNED_IMPORTS = (
    "yfinance", "app.marketdata.providers", "app.marketdata.jobs", "app.marketdata.probe", "apscheduler", "sched", "threading",
    "asyncio", "anthropic", "openai", "langchain", "langgraph", "random",
)


def imported(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_no_provider_ingestion_scheduler_or_llm_imports():
    for path in FILES:
        for module in imported(path):
            assert not module.startswith(BANNED_IMPORTS), f"{path.name} imports {module}"


def identifiers(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
    return out


def test_no_order_exchange_or_strategy_activation_code():
    banned = re.compile(r"place_order|submit_order|fill_price|execution_price|MockExchange|mock_exchange|activat|rank|optimi[sz]", re.IGNORECASE)
    for path in FILES:
        hits = sorted(i for i in identifiers(path) if banned.search(i))
        assert not hits, f"{path.name}: {hits}"


def test_the_api_only_reads_results_and_starts_runs():
    methods = {(route.path, method) for route in router.routes for method in route.methods}
    writes = {(p, m) for p, m in methods if m not in ("GET", "HEAD")}
    assert writes == {("/backtests", "POST")}
