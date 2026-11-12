"""Phase 6 architecture boundaries: the LLM explains, it never trades.

  * the LLM, explanation and journal packages (and their API routers) import no order, exchange-mutating, risk-engine,
    risk-setting, agent, backtest-runner, provider, scheduler, thread or broker code;
  * they never construct or write orders, fills, positions, ledger entries, risk records or agent rows, and never call
    any order-, risk- or lifecycle-changing method;
  * the LLM package sees only settings and enums (no database, no services);
  * nothing below the API imports Phase 6, so trading behaviour cannot depend on it;
  * no mandatory LLM, scheduler or queue dependency was added.
"""

import ast
import pathlib
import re

from tests.agent.test_boundaries import code, imports, sources

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
BACKEND = APP.parent
PHASE6 = [APP / "llm", APP / "explanations", APP / "journal", APP / "api/v1/explanations.py", APP / "api/v1/journal.py",
          APP / "api/v1/schemas_journal.py"]


def phase6_sources():
    for root in PHASE6:
        yield from sources(root)


def test_phase6_imports_no_trading_side_effect_code():
    banned = ("app.orders", "app.exchange.service", "app.exchange.costs", "app.exchange.accounting",
              "app.exchange.corporate_actions", "app.exchange.dividends", "app.risk", "app.agent", "app.backtest",
              "app.backtest_runs", "app.ops", "app.marketdata.providers", "app.marketdata.jobs", "app.marketdata.pipeline",
              "app.api.v1.trading", "app.api.v1.agent", "app.api.v1.operator", "app.api.v1.market", "yfinance",
              "threading", "asyncio", "concurrent", "multiprocessing", "sched", "apscheduler", "celery", "redis", "kafka",
              "subprocess", "kiteconnect", "langchain", "langgraph", "openai")
    for path in phase6_sources():
        bad = sorted(m for m in imports(path) if m.startswith(banned))
        assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_phase6_never_writes_or_changes_trading_state():
    models = ("Order", "Fill", "Position", "CashLedgerEntry", "RiskDecision", "RiskSettings", "RiskState",
              "ProtectiveExitRequirement", "CorporateActionAdjustment", "DividendCredit", "AgentInstance",
              "AgentConfigVersion", "AgentCycle", "AgentDecision", "AgentEvent", "AgentInstrumentState", "Candle",
              "VirtualAccount", "BacktestRun")
    names = "|".join(models)
    for path in phase6_sources():
        text = code(path)
        assert not re.search(rf"\b({names})\(", text), path.name
        assert not re.search(rf"\b(insert|update|delete)\(\s*({names})\b", text), path.name
        assert not re.search(
            r"\.(submit|cancel|cancel_all_open|process_open_orders|run_protective_exits|set_kill_switch|save_settings|"
            r"refresh|transition|record_event|configure|start|pause|resume|stop|engage_kill_switch)\(", text), path.name
        assert not re.search(r"\b(OrderService|MockExchange|RiskService|OrderIntent|TradingContext)\b", text), path.name


def test_the_llm_package_sees_only_settings_and_enums():
    allowed = ("app.core.config", "app.db.enums", "app.llm")
    for path in sources(APP / "llm"):
        own = {m for m in imports(path) if m.startswith("app")}
        assert all(m.startswith(allowed) for m in own), f"{path.name} imports {sorted(own)}"
        assert "Session" not in code(path), path.name


def test_the_journal_and_explanations_write_only_their_own_tables():
    own = {"JournalEntry", "JournalTag", "JournalEntryTag", "JournalReflection", "TradeExplanation", "AuditEvent"}
    for path in phase6_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("add", "delete")
                    and ast.unparse(node.func.value) in ("db", "self.db", "svc.db")):
                target = node.args[0] if node.args else None
                if isinstance(target, ast.Call) and isinstance(target.func, ast.Name):
                    assert target.func.id in own, f"{path.name}: db.{node.func.attr}({target.func.id}(...))"


def test_lower_layers_never_import_phase6():
    for pkg in ("agent", "strategies", "indicators", "backtest", "backtest_runs", "marketdata", "exchange", "orders", "risk",
                "ops", "db", "core"):
        for path in sources(APP / pkg):
            bad = sorted(m for m in imports(path) if m.startswith(("app.journal", "app.explanations", "app.llm")))
            assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_no_mandatory_llm_scheduler_or_queue_dependency():
    reqs = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    for name in ("anthropic", "openai", "langchain", "langgraph", "celery", "redis", "apscheduler", "rq"):
        assert not re.search(rf"^{name}\b", reqs, re.M), name


def test_phase6_never_schedules_work():
    for path in phase6_sources():
        text = code(path)
        assert not re.search(r"while True|Thread\(|create_task|BackgroundTasks|add_task|threading", text), path.name
