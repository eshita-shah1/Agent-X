"""Phase 3 backtesting and Phase 4 execution stay separate (integration of Phase 3 with Phase 4).

The Phase 3 backtest simulates its own NEXT_BAR_OPEN_V1 fills: it never imports, calls or routes orders through the
Phase 4 risk engine, Mock Exchange, order pipeline or operations code, and never uses their execution policies or fill
prices. Phase 4 execution never reads backtest results (results are advisory, decision D15). Phase 4 keeps its own
execution vocabulary inside its own packages.
"""

import ast
import pathlib
import subprocess
import sys

from app.backtest import FILL_MODEL, run_backtest
from tests.backtest import NEXT_DAY, run, session

BACKEND = pathlib.Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
PHASE3 = [APP / "indicators", APP / "strategies", APP / "backtest", APP / "backtest_runs",
          APP / "api" / "v1" / "backtests.py", APP / "api" / "v1" / "schemas_backtest.py"]
PHASE4 = [APP / "exchange", APP / "orders", APP / "risk", APP / "ops",
          APP / "api" / "v1" / "trading.py", APP / "api" / "v1" / "operator.py"]
PHASE4_MODULES = ("app.exchange", "app.orders", "app.risk", "app.ops", "app.api.v1.trading", "app.api.v1.operator",
                  "app.db.models.trading")
PHASE3_MODULES = ("app.backtest", "app.backtest_runs", "app.api.v1.backtests")


def sources(roots):
    for root in roots:
        yield from ([root] if root.is_file() else sorted(root.rglob("*.py")))


def imports(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_both_sides_exist():
    assert all(p.exists() for p in PHASE3) and all(p.exists() for p in PHASE4)


def test_phase3_code_never_imports_phase4_execution():
    for path in sources(PHASE3):
        bad = sorted(m for m in imports(path) if m.startswith(PHASE4_MODULES))
        assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_phase4_execution_never_imports_the_backtest():
    for path in sources(PHASE4):
        bad = sorted(m for m in imports(path) if m.startswith(PHASE3_MODULES))
        assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_loading_the_backtest_loads_no_execution_code():
    probe = (
        "import sys, app.backtest, app.backtest_runs.jobs, app.api.v1.backtests; "
        f"print(sorted(m for m in sys.modules if m.startswith({PHASE4_MODULES[:-1]!r})))"
    )
    out = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_the_backtest_uses_only_its_own_fill_model():
    assert FILL_MODEL == "NEXT_BAR_OPEN_V1"
    phase4_policies = ("VERIFIED_NEXT_BAR_OPEN", "SIMULATION_PROVIDER_NATIVE", "ExecutionPolicy", "execution_mode")
    for path in sources(PHASE3):
        text = path.read_text(encoding="utf-8")
        assert not any(p in text for p in phase4_policies), path.relative_to(APP)
    result = run(session(NEXT_DAY), {"10:00": None})
    assert result.trades and all(t.exit_reason for t in result.trades)


def test_the_backtest_needs_no_execution_configuration():
    """A backtest runs with Phase 4 execution disabled (its default): it never passes through the execution gate."""
    from app.core.config import Settings

    assert Settings.model_fields["execution_mode"].default == "disabled"
    assert "settings" not in run_backtest.__code__.co_varnames
