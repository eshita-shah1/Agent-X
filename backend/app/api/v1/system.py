"""Phase 8: operational status for authenticated users and operators. Read-only.

`/system/status` separates what affects readiness (database, migration compatibility) from what is only reported
(execution configuration, market-data freshness, the optional LLM provider, the caller's own agent). `/system/agents` is
the operator's view of every agent's cycles, heartbeat and failures. No secret or credential is ever returned.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.operator import require_operator
from app.core.clock import Clock, utc_now
from app.core.config import Settings
from app.db.models import AgentInstance, User
from app.db.scoping import owned_select
from app.llm.narrator import Narrator
from app.monitoring import health

router = APIRouter(prefix="/system", tags=["system"])


def get_clock(request: Request) -> Clock:
    return getattr(request.app.state, "clock", utc_now)


@router.get("/status")
def system_status(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)) -> dict:
    now: datetime = clock()
    db_check = health.database_check(request.app.state.engine)
    llm = Narrator(settings).status()
    agent = db.scalar(owned_select(AgentInstance, user.id))
    core_ok = db_check["database"] == "ok" and db_check["migration"]["compatible"]
    return {
        "overall": "ok" if core_ok else "unavailable",
        "environment": settings.environment,
        "readiness": {"database": db_check["database"], "migration": db_check["migration"]},
        "reported": {
            "execution": health.execution_check(settings, now),
            "market_data": health.market_data_check(db, now, settings),
            "llm": {"enabled": llm["enabled"], "provider": llm["provider"], "model": llm["model"],
                    "configuration_problems": llm["configuration_problems"], "affects_readiness": False},
            "agent": health.agent_health(agent, db, now, settings) if agent else None,
        },
        "checked_at": now,
    }


@router.get("/agents")
def agents_health(_: User = Depends(require_operator), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings_dep), clock: Clock = Depends(get_clock)) -> dict:
    """Operator-only: every agent's lifecycle, heartbeat, last successful cycle, failed/interrupted cycles (24 h),
    consecutive failures and stale-worker flag, from the Phase 5 records. Agents are identified by id, not by email."""
    now = clock()
    agents = db.scalars(select(AgentInstance).order_by(AgentInstance.id)).all()
    rows = [health.agent_health(a, db, now, settings) for a in agents]
    return {"agents": rows, "needing_attention": [r["agent_id"] for r in rows if r["attention"]], "checked_at": now}
