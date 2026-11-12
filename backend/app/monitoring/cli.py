"""`python -m app.monitoring.cli health [--json]`: a read-only health probe for an external monitor or scheduler.

Exit codes: 0 ready and no agent needs attention; 1 ready but at least one agent needs attention (ERROR, stale worker,
consecutive failures, interrupted cycles); 2 not ready (database unreachable or schema not at this code's head).
Execution being disabled is reported, never a failure. Prints no secrets.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import get_settings
from app.db.models import AgentInstance
from app.db.session import make_engine
from app.monitoring import health


def collect() -> tuple[int, dict]:
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    now = utc_now()
    try:
        db_check = health.database_check(engine)
        report = {"environment": settings.environment, "database": db_check["database"],
                  "migration": db_check["migration"], "execution": health.execution_check(settings, now)}
        if db_check["database"] != "ok" or not db_check["migration"]["compatible"]:
            return 2, report
        with Session(engine) as db:
            report["market_data"] = health.market_data_check(db, now, settings)
            report["agents"] = [health.agent_health(a, db, now, settings)
                                for a in db.scalars(select(AgentInstance).order_by(AgentInstance.id))]
        return (1 if any(a["attention"] for a in report["agents"]) else 0), report
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.monitoring.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health", help="read-only health probe (exit 0 ok, 1 attention, 2 not ready)")
    ap.parse_args(argv)
    code, report = collect()
    print(json.dumps(report, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
