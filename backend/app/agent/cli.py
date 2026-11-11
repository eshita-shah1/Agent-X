"""Agent worker commands. Run from the backend directory:

    python -m app.agent.cli cycle [--agent-id ID]     one cycle for every configured agent (or one), then exit
    python -m app.agent.cli status                    every agent's persisted status, mode and last heartbeat

Scheduling (approved decision D8): Agent-X never schedules itself. `cycle` is a one-shot, idempotent command meant to
be run by an external scheduler a minute or two after each ingestion run (scripts/windows/agent_cycle.ps1). It never
starts ingestion: fresh bars arrive only through the operator-scheduled Phase 2 ingestion.
Exit codes of `cycle`: 0 every cycle succeeded or was skipped (another worker held it), 1 at least one cycle failed.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.agent.cycle import run_all, run_cycle, worker_identity
from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import AgentInstance
from app.db.session import make_engine, make_sessionmaker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agent.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    cycle = sub.add_parser("cycle")
    cycle.add_argument("--agent-id", type=int)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging()
    engine = make_engine(settings.database_url, settings.db_connect_timeout_seconds)
    factory = make_sessionmaker(engine)
    try:
        if args.command == "cycle":
            if args.agent_id is not None:
                results = [run_cycle(engine, factory, settings, utc_now, args.agent_id, worker_identity())]
            else:
                results = run_all(engine, factory, settings, utc_now)
            print(json.dumps(results, default=str, indent=2))
            return 1 if any(r["status"] == "FAILED" for r in results) else 0
        with factory() as s:
            rows = [
                {"agent_id": a.id, "user_id": str(a.user_id), "status": a.status.value,
                 "mode": a.mode.value if a.mode else None, "reason": a.status_reason,
                 "last_heartbeat_at": a.last_heartbeat_at, "consecutive_failures": a.consecutive_failures}
                for a in s.scalars(select(AgentInstance).order_by(AgentInstance.id))
            ]
        print(json.dumps(rows, default=str, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
