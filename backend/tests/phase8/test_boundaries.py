"""Phase 8 boundaries: hardening adds no trading authority.

  * monitoring, health and status code is read-only: it never adds, commits or changes rows (the only write on those
    routes is the existing operator-denial audit, made by require_operator) and never calls trading actions;
  * execution is still disabled by default and the production rules do not touch execution settings;
  * the backup tooling never targets the main, test or staging database for a restore;
  * no scheduler, queue, thread or broker dependency was introduced.
"""

import pathlib
import re

from app.core.config import Settings
from tests.agent.test_boundaries import code, imports, sources

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
BACKEND = APP.parent
READ_ONLY = [APP / "monitoring", APP / "api/v1/system.py"]
PHASE8 = READ_ONLY + [APP / "core/http.py", APP / "core/logging.py", APP / "ops/backup.py"]


def phase8_sources(roots):
    for root in roots:
        yield from sources(root)


def test_monitoring_and_status_are_read_only():
    for path in phase8_sources(READ_ONLY):
        text = code(path)
        assert not re.search(r"\.(add|add_all|delete|commit|merge|flush)\(", text), path.name
        assert not re.search(r"\b(insert|update|delete)\(", text), path.name
        assert not re.search(r"\.(submit|cancel|process_open_orders|run_protective_exits|set_kill_switch|save_settings|"
                             r"transition|start|pause|resume|stop|configure)\(", text), path.name


def test_phase8_imports_no_trading_side_effects_or_background_machinery():
    banned = ("app.orders", "app.exchange.service", "app.risk.service", "app.agent.cycle", "app.agent.pipeline",
              "threading", "asyncio", "concurrent", "multiprocessing", "sched", "apscheduler", "celery", "redis",
              "kiteconnect", "openai", "anthropic")
    for path in phase8_sources(PHASE8):
        bad = sorted(m for m in imports(path) if m.startswith(banned))
        # monitoring reads the execution readiness and the session-date helper only
        bad = [m for m in bad if m != "app.exchange.service" or path.name != "health.py"]
        assert not bad, f"{path.relative_to(APP)} imports {bad}"


def test_execution_stays_disabled_by_default_and_production_rules_never_enable_it():
    assert Settings.model_fields["execution_mode"].default == "disabled"
    source = (APP / "core/config.py").read_text(encoding="utf-8")
    body = source.split("def production_config_problems", 1)[1].split("@model_validator", 1)[0]
    assert "execution_mode" not in body and "execution_approval" not in body


def test_restores_only_target_disposable_databases():
    text = code(APP / "ops/backup.py")
    assert 'RESTORE_SUFFIX = "_restore_check"' in (APP / "ops/backup.py").read_text(encoding="utf-8")
    assert "restore_target_problem(settings, target)" in text and "pg_restore" in text


def test_no_new_runtime_dependencies_for_hardening():
    reqs = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    for name in ("redis", "celery", "apscheduler", "slowapi", "limits", "sentry", "kubernetes", "boto3"):
        assert not re.search(rf"^{name}\b", reqs, re.M), name
