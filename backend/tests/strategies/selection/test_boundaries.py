"""The selection package stays pure and never ranks by outcomes."""

import ast
import pathlib
import re
import subprocess
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[3] / "app" / "strategies" / "selection"
BACKEND = PACKAGE.parents[2]
STDLIB_ALLOWED = {"__future__", "dataclasses", "datetime", "enum", "math", "typing"}  # nothing else
APP_ALLOWED = ("app.strategies", "app.indicators", "app.marketdata.calendar")
FILES = {"__init__.py", "regime.py", "candidates.py", "policy.py", "selector.py", "session.py"}


def imported_modules(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_modules_import_only_the_standard_library_indicators_strategies_and_calendar():
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} == FILES
    for path in files:
        for module in imported_modules(path):
            if module.startswith("app"):
                assert module.startswith(APP_ALLOWED), f"{path.name} imports {module}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, f"{path.name} imports {module}"


def test_importing_the_package_loads_no_infrastructure():
    probe = (
        "import sys, app.strategies.selection; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy','fastapi','starlette','pydantic','psycopg','numpy','pandas','yfinance','requests','httpx',"
        "'curl_cffi','anthropic','openai','langchain','langgraph'} "
        "or m.startswith(('app.db','app.api','app.core','app.backtest','app.marketdata.service','app.marketdata.providers',"
        "'app.marketdata.jobs')))))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_no_outcome_metrics_execution_or_randomness():
    banned = re.compile(
        r"\b(win_rate|winrate|profit\w*|pnl|sharpe|sortino|drawdown|equity|total_return|annual_return|cumulative_return|"
        r"performance|backtest\w*|"
        r"place_order|submit_order|execution_price|fill_price|random)\b",
        re.IGNORECASE,
    )
    for path in PACKAGE.glob("*.py"):
        match = banned.search(path.read_text(encoding="utf-8"))
        assert match is None, f"{path.name}: {match.group(0) if match else ''}"


def test_adx_is_not_reimplemented():
    formula = re.compile(r"\b(fsum|sqrt|plus_dm|minus_dm|true_range)\b|100\.0 \*")
    for path in PACKAGE.glob("*.py"):
        assert not formula.search(path.read_text(encoding="utf-8")), path.name
