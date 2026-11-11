"""Rules other packages apply because of the agent's state.

Risk settings may change only while the account's agent is not RUNNING (approved product decision): a running agent's
cycle sizes entries against the limits in force, so they must not change underneath it. The caller holds the account
lock, which the worker also takes before it reads the limits, so the check cannot race a cycle.
"""

import uuid

from sqlalchemy.orm import Session

from app.db.enums import AgentStatus
from app.db.models import AgentInstance
from app.db.scoping import owned_select


def risk_settings_change_problem(db: Session, user_id: uuid.UUID) -> str | None:
    agent = db.scalar(owned_select(AgentInstance, user_id))
    if agent is not None and agent.status is AgentStatus.RUNNING:
        return "AGENT_RUNNING: pause or stop the agent before changing risk settings"
    return None
