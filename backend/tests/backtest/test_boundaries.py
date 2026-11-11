"""The backtest engine stays pure and separate: no database, API, account, Mock Exchange, broker, clock, randomness
or LLM; strategies, indicators and selection never import it (results can never feed selection, decision D15)."""

import ast
import pathlib
import re
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
PACKAGE = APP / "backtest"
FILES = {"__init__.py", "benchmark.py", "costs.py", "data.py", "engine.py", "fingerprint.py", "model.py", "simulation.py"}
STDLIB_ALLOWED = {"__future__", "collections", "dataclasses", "datetime", "decimal", "enum", "hashlib", "json", "types", "typing"}
APP_ALLOWED = ("app.backtest", "app.strategies", "app.indicators", "app.marketdata.calendar")


def imported_modules(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_modules_import_only_the_standard_library_strategies_indicators_and_calendar():
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} == FILES
    for path in files:
        for module in imported_modules(path):
            if module.startswith("app"):
                assert module.startswith(APP_ALLOWED), f"{path.name} imports {module}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, f"{path.name} imports {module}"


def test_importing_the_engine_loads_no_infrastructure():
    probe = (
        "import sys, app.backtest; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy','fastapi','starlette','pydantic','psycopg','numpy','pandas','yfinance','requests','httpx',"
        "'curl_cffi','anthropic','openai','langchain','langgraph'} "
        "or m.startswith(('app.db','app.api','app.core','app.marketdata.service','app.marketdata.providers',"
        "'app.marketdata.jobs','app.marketdata.archive')))))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_strategies_indicators_and_selection_never_import_the_backtest():
    for root in (APP / "strategies", APP / "indicators"):
        for path in root.rglob("*.py"):
            assert not any(m.startswith("app.backtest") for m in imported_modules(path)), path


def test_no_clock_randomness_account_or_order_code():
    banned = re.compile(
        r"\b(random|uuid|time\.time|perf_counter|VirtualAccount|virtual_accounts|initial_capital|place_order|submit_order|"
        r"fill_price|execution_price|order_id|MockExchange|mock_exchange|broker_\w+)\b|\.now\(|\.today\(",
    )
    for path in PACKAGE.glob("*.py"):
        match = banned.search(path.read_text(encoding="utf-8"))
        assert match is None, f"{path.name}: {match.group(0) if match else ''}"


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


def test_no_code_activates_ranks_or_optimizes_strategies():
    banned = re.compile(r"activat|auto_select|rank|best_|optimi[sz]", re.IGNORECASE)
    for path in PACKAGE.glob("*.py"):
        hits = sorted(i for i in identifiers(path) if banned.search(i))
        assert not hits, f"{path.name}: {hits}"
