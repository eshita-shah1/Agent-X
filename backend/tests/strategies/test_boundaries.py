"""The strategy package stays pure: standard library, the approved indicators and the stdlib-only calendar."""

import ast
import pathlib
import re
import subprocess
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "app" / "strategies"
BACKEND = PACKAGE.parents[1]
STDLIB_ALLOWED = {"__future__", "dataclasses", "datetime", "enum", "sys", "types", "typing"}
APP_ALLOWED = ("app.strategies", "app.indicators", "app.marketdata.calendar")
FILES = {"__init__.py", "base.py", "params.py", "orb.py", "vwap.py", "ema_crossover.py", "rsi_bollinger.py", "supertrend.py", "registry.py"}


def imported_modules(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_modules_import_only_the_standard_library_indicators_and_calendar():
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} == FILES  # no selection, regime or backtest module in Step 2
    for path in files:
        for module in imported_modules(path):
            if module.startswith("app"):
                assert module.startswith(APP_ALLOWED), f"{path.name} imports {module}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, f"{path.name} imports {module}"


def test_importing_the_package_loads_no_database_api_network_numeric_or_llm_module():
    probe = (
        "import sys, app.strategies; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy','fastapi','starlette','pydantic','psycopg','numpy','pandas','yfinance','requests','httpx',"
        "'curl_cffi','anthropic','openai','langchain','langgraph'} "
        "or m.startswith(('app.db','app.api','app.core','app.backtest','app.marketdata.service','app.marketdata.providers',"
        "'app.marketdata.jobs')))))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_no_execution_short_selling_randomness_or_data_filling_in_the_package():
    banned = re.compile(
        r"\b(random|fillna|ffill|interpolate|place_order|submit_order|execution_price|fill_price|SELL_SHORT|ENTER_SHORT|SHORT)\b"
    )
    for path in PACKAGE.glob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), path.name


def test_strategies_do_not_reimplement_indicator_formulas():
    """Formulas live in app.indicators; strategy modules hold no smoothing, deviation or range arithmetic."""
    formula = re.compile(r"\b(fsum|sqrt|alpha)\b|\(\s*n\s*-\s*1\s*\)|\* \(self\.period - 1\)")
    for path in PACKAGE.glob("*.py"):
        assert not formula.search(path.read_text(encoding="utf-8")), path.name
