"""One agent cycle: a bounded, one-shot unit of work, run by an external scheduler (approved decision D8).

There is no loop, thread or in-app scheduler. `python -m app.agent.cli cycle` runs one cycle for every configured
agent and exits; Windows Task Scheduler (or cron) runs it every few minutes, after ingestion. For each agent:

  lock   a PostgreSQL session advisory lock on the agent id, held on its own connection for the whole cycle. A second
         worker skips the agent; a crashed worker's lock disappears with its connection.
  T0     any cycle still RUNNING belongs to a dead worker: it is marked INTERRUPTED. A new RUNNING cycle is recorded.
  T1     Phase 4 duties, for every agent whatever its status (D10): process resting orders, refresh risk, run
         protective exits. Committed on its own, so a strategy-side failure can never stop protective exits.
  T2     under the account lock, then the agent row lock: re-read status and kill switch, reconcile, replay each
         instrument, record decisions, submit orders through OrderService, advance the watermarks. One transaction:
         a crash before commit leaves no order and no decision behind, and the next cycle redoes the same work with
         the same idempotency keys.
  fail   T2 rolls back; the cycle is FAILED; after AGENT_MAX_CONSECUTIVE_FAILURES failures the agent enters ERROR.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import asdict
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent import configuration, lifecycle
from app.agent.pipeline import DecisionPipeline, InstrumentReport
from app.agent.positions import FillPoint, assess, contexts_for_bars, flat_contexts, session_fills
from app.agent.replay import ReplayError, run_replay, warmup_anchor
from app.core.config import Settings
from app.core.logging import bind_log_context, log_context
from app.db.enums import AgentCycleStatus, AgentInstrumentStatus, AgentMode, AgentStatus
from app.db.models import AgentCycle, AgentInstance, AgentInstrumentState
from app.db.scoping import owned_select
from app.exchange.readiness import execution_readiness
from app.exchange.service import lock_account, session_date_of
from app.marketdata.errors import InstrumentDisabled
from app.marketdata.providers.registry import ProviderRegistry
from app.marketdata.service import MarketDataService
from app.orders.service import OrderService, TradingContext

log = logging.getLogger("agentx.agent")

LOCK_CLASS = 0x41474E54  # "AGNT": the advisory-lock namespace of agent cycles


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def _try_lock(conn, agent_id: int) -> bool:
    return bool(conn.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": LOCK_CLASS, "o": agent_id}).scalar())


def _unlock(conn, agent_id: int) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(:c, :o)"), {"c": LOCK_CLASS, "o": agent_id})


def agent_ids(factory: sessionmaker[Session]) -> list[int]:
    with factory() as s:
        return list(s.scalars(select(AgentInstance.id).order_by(AgentInstance.id)))


def run_all(engine: Engine, factory: sessionmaker[Session], settings: Settings, clock, registry: ProviderRegistry | None = None,
            worker_id: str | None = None) -> list[dict]:
    worker_id = worker_id or worker_identity()
    return [run_cycle(engine, factory, settings, clock, agent_id, worker_id, registry) for agent_id in agent_ids(factory)]


def run_cycle(engine: Engine, factory: sessionmaker[Session], settings: Settings, clock, agent_id: int, worker_id: str,
              registry: ProviderRegistry | None = None) -> dict:
    with engine.connect() as lock_conn:
        if not _try_lock(lock_conn, agent_id):
            lock_conn.commit()
            return {"agent_id": agent_id, "status": "SKIPPED_LOCKED"}
        lock_conn.commit()
        try:
            with log_context(agent_id=agent_id, worker_id=worker_id):  # every log line of the cycle carries them
                return _locked_cycle(factory, settings, clock, agent_id, worker_id, registry)
        finally:
            _unlock(lock_conn, agent_id)
            lock_conn.commit()


def _locked_cycle(factory, settings, clock, agent_id, worker_id, registry) -> dict:
    # T0: claim the cycle
    with factory() as s:
        agent = s.get(AgentInstance, agent_id)
        now = clock()
        for stale in s.scalars(
            owned_select(AgentCycle, agent.user_id).where(AgentCycle.agent_id == agent.id, AgentCycle.status == AgentCycleStatus.RUNNING)
        ):
            stale.status, stale.finished_at = AgentCycleStatus.INTERRUPTED, now
            stale.error_code, stale.error_message = "INTERRUPTED", "the worker stopped before finishing this cycle"
            lifecycle.record_event(s, agent, "agent.cycle_interrupted", "WORKER", now, cycle_id=stale.id,
                                   details={"worker_id": stale.worker_id, "started_at": stale.started_at.isoformat()})
        cycle = AgentCycle(user_id=agent.user_id, agent_id=agent.id, status=AgentCycleStatus.RUNNING, worker_id=worker_id,
                           agent_status=agent.status, mode=agent.mode, session_date=session_date_of(now), started_at=now)
        s.add(cycle)
        s.commit()
        cycle_id, user_id = cycle.id, agent.user_id
    bind_log_context(cycle_id=cycle_id)

    summary: dict = {}
    # T1: Phase 4 duties
    try:
        with factory() as s:
            ctx = TradingContext.build(s, settings, clock)
            account = lock_account(s, user_id)
            changed = ctx.exchange.process_open_orders(account)
            risk = ctx.risk.refresh(account)
            exits = OrderService(ctx).run_protective_exits(user_id)
            s.commit()
            summary["phase4"] = {"orders_changed": len(changed), "protective_exits_submitted": len(exits),
                                 "risk_breaches": risk.breaches}
    except Exception as exc:  # recorded, never swallowed silently
        return _fail(factory, clock, agent_id, cycle_id, "PHASE4_DUTIES_FAILED", exc, summary, settings)

    # T2: orchestration
    try:
        with factory() as s:
            summary.update(_orchestrate(s, settings, clock, agent_id, cycle_id, worker_id, registry))
            cycle = s.get(AgentCycle, cycle_id)
            cycle.status, cycle.finished_at, cycle.summary = AgentCycleStatus.SUCCEEDED, clock(), summary
            s.commit()
    except Exception as exc:
        return _fail(factory, clock, agent_id, cycle_id, "ORCHESTRATION_FAILED", exc, summary, settings)
    return {"agent_id": agent_id, "cycle_id": cycle_id, "status": "SUCCEEDED", "summary": summary}


def _fail(factory, clock, agent_id, cycle_id, code, exc, summary, settings) -> dict:
    log.exception("agent cycle failed", extra={"fields": {"agent_id": agent_id, "cycle_id": cycle_id, "code": code}})
    with factory() as s:
        s.rollback()
        agent = s.scalar(select(AgentInstance).where(AgentInstance.id == agent_id).with_for_update())
        now = clock()
        cycle = s.get(AgentCycle, cycle_id)
        cycle.status, cycle.finished_at, cycle.summary = AgentCycleStatus.FAILED, now, summary
        cycle.error_code, cycle.error_message = code, f"{type(exc).__name__}: {exc}"[:4000]
        agent.consecutive_failures += 1
        agent.last_heartbeat_at, agent.last_cycle_id = now, cycle_id
        lifecycle.record_event(s, agent, "agent.cycle_failed", "WORKER", now, cycle_id=cycle_id,
                               details={"code": code, "error": cycle.error_message, "consecutive": agent.consecutive_failures})
        if agent.consecutive_failures >= settings.agent_max_consecutive_failures and agent.status in (AgentStatus.RUNNING, AgentStatus.PAUSED):
            lifecycle.transition(s, agent, AgentStatus.ERROR, f"{agent.consecutive_failures} consecutive failed cycles",
                                 "WORKER", now, event_type="agent.error", cycle_id=cycle_id)
        s.commit()
    return {"agent_id": agent_id, "cycle_id": cycle_id, "status": "FAILED", "error": f"{code}: {exc}"}


def _orchestrate(s: Session, settings: Settings, clock, agent_id: int, cycle_id: int, worker_id: str, registry) -> dict:
    now = clock()
    today = session_date_of(now)
    agent0 = s.get(AgentInstance, agent_id)
    account = lock_account(s, agent0.user_id)
    agent = configuration.get_agent(s, agent0.user_id, lock=True)
    agent.worker_id, agent.last_heartbeat_at, agent.last_cycle_id = worker_id, now, cycle_id
    agent.consecutive_failures = 0
    out: dict = {"agent_status": agent.status.value, "mode": agent.mode.value if agent.mode else None}
    if agent.status in (AgentStatus.STOPPED, AgentStatus.ERROR):
        out["evaluation"] = f"skipped: the agent is {agent.status.value} (Phase 4 duties only)"
        return out

    ctx = TradingContext.build(s, settings, clock)
    risk_state = ctx.risk.get_state(agent.user_id, create=False)
    kill = bool(risk_state and risk_state.kill_switch_engaged)
    if kill and agent.status is AgentStatus.RUNNING:
        lifecycle.transition(s, agent, AgentStatus.PAUSED, f"KILL_SWITCH_ENGAGED: {risk_state.kill_switch_reason}",
                             "WORKER", now, event_type="agent.paused_by_kill_switch", cycle_id=cycle_id)
    if agent.mode is AgentMode.EXECUTION and agent.status is AgentStatus.RUNNING:
        readiness = execution_readiness(settings, today)
        if not readiness.enabled:
            lifecycle.transition(s, agent, AgentStatus.PAUSED, "EXECUTION_UNAVAILABLE: " + "; ".join(readiness.blocking_reasons),
                                 "WORKER", now, event_type="agent.paused_execution_unavailable", cycle_id=cycle_id)

    version = configuration.effective_version(s, agent, today)
    if version is None:
        lifecycle.transition(s, agent, AgentStatus.ERROR, "no configuration is effective for today's session", "WORKER",
                             now, event_type="agent.error", cycle_id=cycle_id)
        return out
    problems = lifecycle.discrepancies(s, agent, today)
    if problems:
        lifecycle.record_event(s, agent, "agent.reconciliation_failed", "WORKER", now, cycle_id=cycle_id,
                               details={"discrepancies": problems})
        lifecycle.transition(s, agent, AgentStatus.ERROR, "RECONCILIATION_FAILED: " + "; ".join(problems)[:1000], "WORKER",
                             now, event_type="agent.error", cycle_id=cycle_id)
        out["discrepancies"] = problems
        return out

    svc = MarketDataService(s, registry or ProviderRegistry({}), settings, clock)
    policy = configuration.policy_of(version)
    pipeline = DecisionPipeline(s, ctx, agent, version, cycle_id, account, today, now, kill)

    def event(event_type, instrument_id, details):
        lifecycle.record_event(s, agent, event_type, "WORKER", now, cycle_id=cycle_id, instrument_id=instrument_id, details=details)

    reports = []
    for iid in version.instrument_ids:
        report = InstrumentReport(iid)
        reports.append(report)
        state = lifecycle.instrument_state(s, agent, iid)
        _refresh_status(s, agent, state, today, event)
        try:
            if state.replay_session != today:
                first, truncated = warmup_anchor(s, svc, iid, today, version.warmup_sessions, now)
                state.replay_session, state.replay_from_session = today, first
                if truncated is not None:
                    report.notes.append(f"warm-up truncated by a corporate action on {truncated}")
            orphan = state.status is AgentInstrumentStatus.ORPHANED_OVERNIGHT

            def contexts(bar_starts, iid=iid, orphan=orphan):
                if orphan:
                    return flat_contexts(bar_starts)
                fills = session_fills(s, agent.user_id, iid, today)
                return contexts_for_bars(bar_starts, [FillPoint(f.price_as_of, f.side, f.quantity) for f, _o in fills])

            result = run_replay(s, svc, policy, iid, today, state.replay_from_session, contexts, now)
        except (ReplayError, InstrumentDisabled, LookupError) as exc:  # contained to this instrument
            if state.status is not AgentInstrumentStatus.DATA_ERROR:
                event("agent.instrument_data_error", iid, {"error": str(exc)})
            state.status, state.status_detail, state.status_session = AgentInstrumentStatus.DATA_ERROR, str(exc)[:2000], today
            pipeline.settle_owed_exit(state, report, event)
            continue
        if state.status is AgentInstrumentStatus.DATA_ERROR:
            state.status, state.status_detail, state.status_session = AgentInstrumentStatus.OK, None, None
            _refresh_status(s, agent, state, today, event)
        if result.decision is not None:
            pipeline.record_selection(iid, result.decision)
        check = ctx.gate.check(iid) if agent.mode is AgentMode.EXECUTION else None
        for outcome in result.outcomes:
            if state.last_bar_start is not None and outcome.archived.start <= state.last_bar_start:
                continue  # processed by an earlier cycle (or before the agent started)
            pipeline.process_bar(state, outcome, check, report)
            state.last_bar_start, state.last_candle_id = outcome.archived.start, outcome.archived.candle_id
        pipeline.settle_owed_exit(state, report, event)
    # an instrument dropped by a newer configuration version may still owe an exit: settle or expire it (D2, D6)
    for state in s.scalars(
        owned_select(AgentInstrumentState, agent.user_id).where(
            AgentInstrumentState.agent_id == agent.id, AgentInstrumentState.owed_exit_since.is_not(None),
            AgentInstrumentState.instrument_id.not_in(version.instrument_ids),
        ).with_for_update()
    ):
        report = InstrumentReport(state.instrument_id)
        reports.append(report)
        _refresh_status(s, agent, state, today, event)
        pipeline.settle_owed_exit(state, report, event)
    s.flush()
    out["instruments"] = [asdict(r) for r in reports]
    out["agent_status_after"] = agent.status.value
    return out


def _refresh_status(s: Session, agent: AgentInstance, state, today: date, event) -> None:
    """Re-derive the instrument's block from the authoritative records. A manual intervention stays for the rest of
    the session in which it was detected, even after the manual position is closed."""
    status, detail = assess(s, agent.user_id, state.instrument_id, today)
    if (status is AgentInstrumentStatus.OK and state.status is AgentInstrumentStatus.MANUAL_INTERVENTION
            and state.status_session == today):
        return
    if status is AgentInstrumentStatus.OK and state.status is AgentInstrumentStatus.DATA_ERROR:
        return  # cleared by the next successful replay
    if status is not state.status:
        event("agent.instrument_status_changed", state.instrument_id,
              {"from": state.status.value, "to": status.value, "detail": detail})
        state.status_session = today if status is not AgentInstrumentStatus.OK else None
    state.status, state.status_detail = status, detail
