"""Read-only health and observability checks (Phase 8), shared by the API and `python -m app.ops.cli health`.

Readiness (is this process able to serve?) is only: the database answers and its schema is at the code's migration head.
Everything else is reported, never fatal: execution disabled is the approved development state, an unavailable
optional LLM provider changes nothing for trading, and market-data staleness is a data condition the price gate
already handles. Agent observability is derived from the Phase 5 records (agent_instances, agent_cycles) with the agent's
own staleness rule; no parallel state is kept. Nothing here writes, and no secret or configuration value is returned
beyond what the authenticated status endpoints already expose.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agent.lifecycle import worker_stale
from app.core.config import Settings
from app.db.enums import AgentCycleStatus, AgentStatus
from app.db.models import AgentCycle, AgentInstance, DataProvider, ProviderHealthState
from app.exchange.readiness import execution_readiness
from app.exchange.service import session_date_of

ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"


@lru_cache
def expected_head() -> str | None:
    """The migration head shipped with this code (read from the alembic scripts), or None if they are missing."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(ALEMBIC_DIR))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception:
        return None


def database_check(engine: Engine) -> dict:
    """{"database": ok|unavailable, "latency_ms", "migration": {current, expected, compatible}}. Never raises."""
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as exc:
        return {"database": "unavailable", "error_category": type(exc).__name__, "migration": {
            "current": None, "expected": expected_head(), "compatible": False}}
    expected = expected_head()
    return {
        "database": "ok", "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "migration": {"current": current, "expected": expected, "compatible": expected is not None and current == expected},
    }


def ready(engine: Engine) -> bool:
    check = database_check(engine)
    return check["database"] == "ok" and check["migration"]["compatible"]


def agent_health(agent: AgentInstance, db: Session, now: datetime, settings: Settings) -> dict:
    """One agent's operational state from its Phase 5 records."""
    since = now - timedelta(hours=24)
    counts = dict(db.execute(
        select(AgentCycle.status, func.count()).where(AgentCycle.agent_id == agent.id, AgentCycle.started_at >= since)
        .group_by(AgentCycle.status)).all())
    last_ok = db.scalar(select(func.max(AgentCycle.finished_at)).where(
        AgentCycle.agent_id == agent.id, AgentCycle.status == AgentCycleStatus.SUCCEEDED))
    last = db.scalar(select(AgentCycle).where(AgentCycle.agent_id == agent.id).order_by(AgentCycle.id.desc()).limit(1))
    stale = worker_stale(agent, now, settings.agent_worker_stale_after_seconds)
    attention = []
    if agent.status is AgentStatus.ERROR:
        attention.append(f"AGENT_ERROR: {agent.status_reason or ''}".rstrip(": "))
    if stale:
        attention.append("WORKER_NOT_SEEN")
    if agent.consecutive_failures:
        attention.append(f"CONSECUTIVE_FAILURES: {agent.consecutive_failures}")
    if counts.get(AgentCycleStatus.INTERRUPTED):
        attention.append(f"INTERRUPTED_CYCLES_24H: {counts[AgentCycleStatus.INTERRUPTED]}")
    return {
        "agent_id": agent.id, "status": agent.status.value, "mode": agent.mode.value if agent.mode else None,
        "status_reason": agent.status_reason, "last_heartbeat_at": agent.last_heartbeat_at,
        "last_successful_cycle_at": last_ok, "last_cycle_status": last.status.value if last else None,
        "last_cycle_error_code": last.error_code if last else None, "consecutive_failures": agent.consecutive_failures,
        "worker_stale": stale,
        "cycles_24h": {s.value: counts.get(s, 0) for s in AgentCycleStatus},
        "attention": attention,
    }


def market_data_check(db: Session, now: datetime, settings: Settings) -> dict:
    out = {}
    for provider, state in db.execute(select(DataProvider, ProviderHealthState).outerjoin(
            ProviderHealthState, ProviderHealthState.provider_id == DataProvider.id)).all():
        last = state.last_successful_ingestion_at if state else None
        out[provider.code] = {
            "last_successful_ingestion_at": last,
            "minutes_since_ingestion": round((now - last).total_seconds() / 60, 1) if last else None,
            "consecutive_failures": state.consecutive_failures if state else 0,
            "unavailable": bool(state and state.consecutive_failures >= settings.marketdata_provider_unavailable_after_failures),
        }
    return out


def execution_check(settings: Settings, now: datetime) -> dict:
    r = execution_readiness(settings, session_date_of(now))
    # Disabled is the approved development state: reported, never a failure.
    return {"mode": settings.execution_mode, "enabled": r.enabled, "blocking_reasons": r.blocking_reasons,
            "is_failure": False}
