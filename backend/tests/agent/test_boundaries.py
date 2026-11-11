"""Phase 5 architecture boundaries (non-negotiable rules of the phase plan).

  * strategies, indicators, selection, the backtest and the Phase 4 services never import the agent;
  * the agent never imports the backtest, providers, network, scheduler, thread, LLM or broker code;
  * the agent places orders only through OrderService.submit and never constructs or writes orders, fills, positions,
    ledger entries or risk records itself, and never calls the Mock Exchange's order-mutating methods;
  * no real-money or broker execution path exists anywhere in the application.
"""

import ast
import pathlib
import re

from app.core.config import Settings

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
AGENT = APP / "agent"


def sources(root: pathlib.Path):
    yield from ([root] if root.is_file() else sorted(root.rglob("*.py")))


def imports(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def code(path: pathlib.Path) -> str:
    """Source without docstrings and comments, so rules apply to code only."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_lower_layers_never_import_the_agent():
    for pkg in ("strategies", "indicators", "backtest", "backtest_runs", "marketdata", "exchange", "orders", "risk", "ops", "db"):
        for path in sources(APP / pkg):
            bad = sorted(m for m in imports(path) if m.startswith("app.agent"))
            assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_the_agent_imports_no_backtest_provider_network_scheduler_thread_llm_or_broker_code():
    banned = ("app.backtest", "app.backtest_runs", "app.api", "app.marketdata.providers.yahoo", "app.marketdata.jobs",
              "app.marketdata.probe", "yfinance", "requests", "httpx", "urllib", "socketserver", "threading", "asyncio",
              "concurrent", "multiprocessing", "sched", "apscheduler", "celery", "redis", "kafka", "anthropic", "openai",
              "langchain", "langgraph", "kiteconnect", "subprocess")
    for path in sources(AGENT):
        bad = sorted(m for m in imports(path) if m.startswith(banned))
        assert not bad, f"{path.name} imports {bad}"


def test_the_agent_places_orders_only_through_order_service():
    for path in sources(AGENT):
        text = code(path)
        assert not re.search(r"\b(Order|Fill|Position|CashLedgerEntry|RiskDecision|RiskSettings|RiskState|ProtectiveExitRequirement)\(", text), path.name
        assert not re.search(r"\b(insert|update|delete)\(\s*(Order|Fill|Position|CashLedgerEntry|RiskDecision|RiskSettings|RiskState)\b", text), path.name
        assert not re.search(r"\.(record_rejected|cancel|cancel_all_open|_execute|_fill_on_bar|_ledger|save_settings)\(", text), path.name
        assert not re.search(r"exchange\.submit\(", text), path.name
        for call in re.findall(r"[\w.]+\.submit\(", text):
            assert call == "self.orders.submit(", f"{path.name}: {call}"
    pipeline = code(AGENT / "pipeline.py")
    assert "self.orders.submit(" in pipeline and "OrderService(ctx)" in pipeline


def test_the_agent_computes_no_risk_cost_or_accounting_formulas():
    """Sizing, risk, costs and accounting stay in Phase 4: the agent imports none of their implementations."""
    phase4_formulas = ("app.risk.engine", "app.exchange.costs", "app.exchange.accounting", "app.exchange.portfolio",
                       "app.exchange.pricing")
    for path in sources(AGENT):
        bad = sorted(m for m in imports(path) if m.startswith(phase4_formulas))
        assert not bad, f"{path.name} imports {bad}"
        text = code(path)
        assert not re.search(r"\b(max_buy_quantity|CostModel|risk_budget|realized_pnl|avg_entry_price\s*=|fee)\b", text), path.name


def test_the_agent_never_reads_backtest_results_or_uses_an_llm():
    for path in sources(AGENT):
        assert not re.search(r"Backtest|backtest_|llm|LLM|prompt", code(path)), path.name


def test_the_agent_never_schedules_itself():
    for path in sources(AGENT):
        text = code(path)
        assert not re.search(r"while True|time\.sleep|threading|Thread\(|create_task|BackgroundTasks|add_task", text), path.name


def test_no_real_money_or_broker_execution_mode_exists():
    modes = Settings.model_fields["execution_mode"].annotation.__args__
    assert set(modes) == {"disabled", "verified_next_bar_open", "simulation_provider_native"}
    for path in sources(APP):
        assert not re.search(r"\b(kiteconnect|KiteConnect|place_live_order|live_trading|real_money\s*=\s*True)\b",
                             path.read_text(encoding="utf-8")), path.relative_to(APP)
