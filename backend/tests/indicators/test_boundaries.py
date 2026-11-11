"""The indicator package stays pure: standard library plus the stdlib-only trading calendar, nothing else."""

import ast
import pathlib
import subprocess
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "app" / "indicators"
BACKEND = PACKAGE.parents[1]
STDLIB_ALLOWED = {"__future__", "collections", "dataclasses", "datetime", "enum", "math", "typing"}
APP_ALLOWED = ("app.indicators", "app.marketdata.calendar")


def imported_modules(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_every_module_imports_only_the_standard_library_and_the_calendar():
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} == {"__init__.py", "core.py", "momentum.py", "volatility.py", "trend.py", "session.py"}
    for path in files:
        for module in imported_modules(path):
            if module.startswith("app"):
                assert module.startswith(APP_ALLOWED), f"{path.name} imports {module}"
            else:
                assert module.split(".")[0] in STDLIB_ALLOWED, f"{path.name} imports {module}"


def test_importing_the_package_loads_no_database_api_network_numeric_or_llm_module():
    probe = (
        "import sys, app.indicators; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy','fastapi','starlette','pydantic','psycopg','numpy','pandas','yfinance','requests','httpx',"
        "'curl_cffi','anthropic','openai','langchain','langgraph'} "
        "or m.startswith(('app.db','app.api','app.core','app.strategies','app.backtest','app.marketdata.service',"
        "'app.marketdata.providers')))))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_no_randomness_nan_or_fill_forward_in_the_package():
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for banned in ("random", "math.nan", 'float("nan")', "float('nan')", "fillna", "ffill", "interpolate"):
            assert banned not in text, f"{path.name} contains {banned!r}"
