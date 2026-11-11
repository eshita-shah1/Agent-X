"""Phase 5 orchestration on PostgreSQL: the agent cycle against the real OrderService, risk engine and Mock Exchange.

Execution is enabled only inside these tests (synthetic fixture provider, synthetic cost schedule, simulation-only
mode). These tests prove the orchestration mechanics; they are not an end-to-end paper-trading run under an approved
execution policy (decision D15 is deferred).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update

from app.agent import lifecycle
from app.agent.cycle import LOCK_CLASS
from app.agent.pipeline import DecisionPipeline
from app.db.enums import (
    AgentCycleStatus,
    AgentDecisionKind,
    AgentInstrumentStatus,
    AgentMode,
    AgentStatus,
    OrderSide,
    OrderSource,
    OrderStatus,
)
from app.db.models import (
    AgentCycle,
    AgentDecision,
    AgentEvent,
    AgentInstrumentState,
    Fill,
    Order,
    Position,
    RiskDecision,
)
from app.exchange.types import OrderIntent
from app.orders.service import OrderService
from tests.integration.agent_support import NEXT_DAY, World
from tests.integration.trading_support import intent, submit

D = Decimal


@pytest.fixture
def world(db, engine, test_settings):
    return World(db, engine, test_settings)


def decisions(w: World, kind=AgentDecisionKind.EVALUATION):
    w.db.expire_all()
    return list(w.db.scalars(select(AgentDecision).where(AgentDecision.kind == kind).order_by(AgentDecision.bar_start, AgentDecision.id)))


def by_hhmm(rows):
    from tests.integration.agent_support import IST

    return {r.bar_start.astimezone(IST).strftime("%H:%M"): r for r in rows}


def orders(w: World):
    w.db.expire_all()
    return list(w.db.scalars(select(Order).order_by(Order.created_at)))


def events(w: World, event_type: str):
    w.db.expire_all()
    return list(w.db.scalars(select(AgentEvent).where(AgentEvent.event_type == event_type)))


def started(w: World, warmup_sessions: int = 0) -> World:
    w.opening_range()
    w.at("09:30")
    w.configure(warmup_sessions=warmup_sessions)
    w.start()
    return w


def breakout(w: World, hhmm: str = "09:30"):
    return w.bar(hhmm, "100.5", "102.5", "100.4", "102")


# ---- the full paper lifecycle ------------------------------------------------------------------------------------


def test_entry_fill_square_off_and_exit_through_order_service(world):
    w = started(world)
    assert w.agent().status is AgentStatus.RUNNING and w.agent().mode is AgentMode.EXECUTION

    breakout(w)
    w.at("09:35")
    assert w.cycle()["status"] == "SUCCEEDED"
    sel = decisions(w, AgentDecisionKind.SELECTION)
    assert len(sel) == 1 and sel[0].config_id == "orb-default" and sel[0].outcome.value == "SELECTED"
    entry = by_hhmm(decisions(w))["09:30"]
    assert entry.outcome.value == "ENTRY_SUBMITTED" and entry.reason == "ORB_BREAKOUT"
    assert entry.candle_id is not None and entry.observation_version == 1 and entry.price_basis == "UNVERIFIED_PROVIDER_NATIVE"
    [order] = orders(w)
    assert order.source is OrderSource.AGENT and order.client_order_id == entry.client_order_id
    assert order.client_order_id.startswith("AG:E:") and order.side is OrderSide.BUY
    assert order.stop_loss_price == D("99") and order.take_profit_price == D("105")  # close 102 + 1.5 x height 2 (D3)
    assert order.status is OrderStatus.PENDING
    rd = w.db.get(RiskDecision, entry.risk_decision_id)
    assert rd.order_id == order.id and order.quantity <= order.requested_quantity  # the risk engine sized it (D4)

    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    pos = w.db.scalar(select(Position))
    assert pos.quantity == order.quantity and pos.opened_at == w.db.get(Fill, 1).price_as_of
    rows = by_hhmm(decisions(w))
    assert rows["09:35"].outcome.value == "NO_SIGNAL" and rows["09:40"].reason == "POSITION_OPEN_NO_EXIT"

    w.flat_bars("09:45", "15:10")
    w.at("15:15")
    w.cycle()
    rows = by_hhmm(decisions(w))
    assert rows["15:10"].reason == "SESSION_SQUARE_OFF" and rows["15:10"].outcome.value == "EXIT_OWED"
    [owed] = decisions(w, AgentDecisionKind.OWED_EXIT)
    sell = w.db.get(Order, owed.order_id)
    assert sell.side is OrderSide.SELL and sell.quantity == pos.quantity and sell.client_order_id.startswith("AG:X:")

    w.flat_bars("15:15", "15:20")
    w.at("15:25")
    w.cycle()
    w.db.expire_all()
    assert w.db.get(Order, sell.id).status is OrderStatus.FILLED
    assert w.db.scalar(select(Position)).quantity == 0
    assert events(w, "agent.owed_exit_satisfied")
    state = w.db.scalar(select(AgentInstrumentState))
    assert state.owed_exit_since is None and state.status is AgentInstrumentStatus.OK

    # repeated cycles over the same data create nothing
    before = (len(orders(w)), w.db.scalar(select(func.count()).select_from(AgentDecision)))
    w.cycle()
    w.cycle()
    assert (len(orders(w)), w.db.scalar(select(func.count()).select_from(AgentDecision))) == before


def test_each_order_has_exactly_one_decision_and_no_direct_writes(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    for o in orders(w):
        assert w.db.scalar(select(func.count()).select_from(AgentDecision).where(AgentDecision.order_id == o.id)) == 1
        assert w.db.scalar(select(func.count()).select_from(RiskDecision).where(RiskDecision.order_id == o.id)) == 1


# ---- entry gating ------------------------------------------------------------------------------------------------


def test_a_late_entry_signal_is_never_acted_on(world):
    w = started(world)
    breakout(w)
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.at("09:40")  # the 09:30 breakout is no longer the latest eligible bar (D5)
    w.cycle()
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SUPPRESSED_LATE_SIGNAL"
    assert orders(w) == []


def test_stale_data_suppresses_entries(world):
    w = started(world)
    breakout(w)
    w.at("09:52")  # bar ended 09:35: 17 minutes old > 15-minute maximum age
    w.cycle()
    row = by_hhmm(decisions(w))["09:30"]
    assert row.outcome.value == "SUPPRESSED_PRICE_NOT_ELIGIBLE" and "exceeds" in row.detail
    assert orders(w) == []


def test_forming_or_not_yet_observed_bars_are_not_used(world):
    w = started(world)
    w.bar("09:30", "100.5", "102.5", "100.4", "102", delay_seconds=120)  # archived, but observed 09:37
    w.at("09:35")
    w.cycle()
    assert decisions(w) == [] and orders(w) == []


def test_missing_bar_suppresses_the_entry_by_the_strategys_gap_rule(world):
    w = started(world)
    w.bar("09:35", "100.5", "102.5", "100.4", "102")  # 09:30 is missing
    w.at("09:40")
    w.cycle()
    row = by_hhmm(decisions(w))["09:35"]
    assert row.reason == "GAP_SUPPRESSED" and orders(w) == []


def test_risk_rejection_is_recorded_with_the_risk_decision(db, engine, test_settings):
    w = World(db, engine, test_settings, risk={"max_risk_per_trade_pct": D("0.0001")})
    started(w)
    breakout(w)
    w.at("09:35")
    w.cycle()
    entry = by_hhmm(decisions(w))["09:30"]
    order = w.db.get(Order, entry.order_id)
    assert entry.outcome.value == "ENTRY_SUBMITTED" and order.status is OrderStatus.REJECTED
    assert entry.order_status == "REJECTED" and "RISK_REJECTED" in entry.detail and entry.risk_decision_id is not None


def test_signals_only_never_submits(db, engine, test_settings, monkeypatch):
    w = World(db, engine, test_settings, execution=False)
    started(w)
    assert w.agent().mode is AgentMode.SIGNALS_ONLY

    def forbidden(*a, **k):
        raise AssertionError("SIGNALS_ONLY must never submit an order")

    monkeypatch.setattr(OrderService, "submit", forbidden)
    breakout(w)
    w.at("09:35")
    assert w.cycle()["status"] == "SUCCEEDED"
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SIGNALS_ONLY_NOT_SUBMITTED"
    assert orders(w) == []


def test_execution_becoming_unavailable_pauses_a_running_agent(world):
    w = started(world)
    w.settings = w.settings.model_copy(update={"execution_mode": "disabled"})
    breakout(w)
    w.at("09:35")
    w.cycle()
    agent = w.agent()
    assert agent.status is AgentStatus.PAUSED and agent.status_reason.startswith("EXECUTION_UNAVAILABLE")
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SUPPRESSED_PAUSED"
    assert orders(w) == []


# ---- pause, kill switch, manual intervention ----------------------------------------------------------------------


def test_pause_suppresses_entries(world):
    w = started(world)
    lifecycle.pause(w.db, w.account.user_id, w.clock())
    w.db.commit()
    breakout(w)
    w.at("09:35")
    w.cycle()
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SUPPRESSED_PAUSED"
    assert orders(w) == []


def test_pause_keeps_exits(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    lifecycle.pause(w.db, w.account.user_id, w.clock())
    w.db.commit()
    w.flat_bars("09:45", "15:10")
    w.at("15:15")
    w.cycle()
    [owed] = decisions(w, AgentDecisionKind.OWED_EXIT)
    assert w.db.get(Order, owed.order_id).side is OrderSide.SELL  # monitor mode still exits


def test_kill_switch_cancels_pending_entry_pauses_and_release_does_not_resume(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    [entry] = orders(w)
    assert entry.status is OrderStatus.PENDING
    lifecycle.engage_kill_switch(w.db, w.ctx(), w.account.user_id, "test emergency", w.clock())
    w.db.commit()
    assert w.db.get(Order, entry.id).status is OrderStatus.CANCELLED
    assert w.agent().status is AgentStatus.PAUSED

    ctx = w.ctx()
    from app.exchange.service import lock_account

    ctx.risk.set_kill_switch(lock_account(w.db, w.account.user_id), False, "released", ctx.exchange)
    w.db.commit()
    w.cycle()
    assert w.agent().status is AgentStatus.PAUSED  # releasing never resumes the agent (D11)
    lifecycle.resume(w.db, w.settings, w.ctx(), w.account.user_id, w.clock())
    w.db.commit()
    assert w.agent().status is AgentStatus.RUNNING


def test_resume_is_refused_while_the_kill_switch_is_engaged(world):
    w = started(world)
    lifecycle.engage_kill_switch(w.db, w.ctx(), w.account.user_id, "stop", w.clock())
    w.db.commit()
    with pytest.raises(lifecycle.AgentRefused, match="KILL_SWITCH_ENGAGED"):
        lifecycle.resume(w.db, w.settings, w.ctx(), w.account.user_id, w.clock())
    w.db.rollback()


def test_a_kill_switch_engaged_elsewhere_pauses_the_agent_on_the_next_cycle(world):
    w = started(world)
    ctx = w.ctx()
    from app.exchange.service import lock_account

    ctx.risk.set_kill_switch(lock_account(w.db, w.account.user_id), True, "via /risk/kill-switch", ctx.exchange)
    w.db.commit()
    breakout(w)
    w.at("09:35")
    w.cycle()
    assert w.agent().status is AgentStatus.PAUSED
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SUPPRESSED_PAUSED"
    assert orders(w) == []


def test_a_manual_order_blocks_the_instrument(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    submit(w.db, w.settings, w.clock, w.account, intent(w.instrument, qty=1, stop_loss_price=D("95")))
    w.cycle()
    state = w.db.scalar(select(AgentInstrumentState))
    assert state.status is AgentInstrumentStatus.MANUAL_INTERVENTION
    assert by_hhmm(decisions(w))["09:30"].outcome.value == "SUPPRESSED_INSTRUMENT_BLOCKED"
    assert [o.source for o in orders(w)] == [OrderSource.MANUAL]


# ---- overnight ------------------------------------------------------------------------------------------------


def test_an_unfilled_square_off_never_carries_over(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    w.flat_bars("09:45", "15:10")
    w.at("15:40")  # the 15:10 bar is stale: the owed exit cannot be submitted
    w.cycle()
    assert by_hhmm(decisions(w))["15:10"].outcome.value == "EXIT_OWED"
    assert decisions(w, AgentDecisionKind.OWED_EXIT) == []

    w.opening_range(NEXT_DAY)
    w.at("09:30", NEXT_DAY)
    w.cycle()
    state = w.db.scalar(select(AgentInstrumentState))
    assert state.status is AgentInstrumentStatus.ORPHANED_OVERNIGHT and state.owed_exit_since is None
    assert events(w, "agent.owed_exit_expired_session_end")
    agent_orders = [o for o in orders(w) if o.source is OrderSource.AGENT]
    assert [o.side for o in agent_orders] == [OrderSide.BUY]  # the agent submits no carry-over SELL (D2)
    # only Phase 4 manages the orphan: the opening range's low (99) touched its stop, so a protective exit fired
    assert all(o.source is OrderSource.RISK_EXIT for o in orders(w) if o.side is OrderSide.SELL)
    w.bar("09:30", "100.5", "102.5", "100.4", "102", NEXT_DAY)
    w.at("09:35", NEXT_DAY)
    w.cycle()
    assert by_hhmm(decisions(w))["09:30"].outcome.value in ("SUPPRESSED_INSTRUMENT_BLOCKED", "SUPPRESSED_POSITION_OR_ORDER")
    assert len([o for o in orders(w) if o.source is OrderSource.AGENT]) == 1


# ---- recovery and concurrency ---------------------------------------------------------------------------------


def test_a_second_worker_skips_an_agent_being_cycled(world):
    w = started(world)
    with w.engine.connect() as other:
        assert other.execute(text("SELECT pg_try_advisory_lock(:c, :o)"), {"c": LOCK_CLASS, "o": w.agent().id}).scalar()
        assert w.cycle()["status"] == "SKIPPED_LOCKED"
        other.execute(text("SELECT pg_advisory_unlock(:c, :o)"), {"c": LOCK_CLASS, "o": w.agent().id})
    assert w.cycle()["status"] == "SUCCEEDED"


def test_a_cycle_left_running_by_a_crashed_worker_is_interrupted(world):
    w = started(world)
    a = w.agent()
    w.db.add(AgentCycle(user_id=a.user_id, agent_id=a.id, status=AgentCycleStatus.RUNNING, worker_id="dead:1",
                        agent_status=a.status, mode=a.mode, session_date=w.day, started_at=w.clock()))
    w.db.commit()
    assert w.cycle()["status"] == "SUCCEEDED"
    statuses = [c.status for c in w.db.scalars(select(AgentCycle).order_by(AgentCycle.id))]
    assert statuses == [AgentCycleStatus.INTERRUPTED, AgentCycleStatus.SUCCEEDED]
    assert events(w, "agent.cycle_interrupted")


def test_a_crash_after_submission_leaves_no_order_and_the_retry_submits_once(world, monkeypatch):
    w = started(world)
    breakout(w)
    w.at("09:35")
    original = DecisionPipeline._decision

    def crash_on_entry(self, *a, **k):
        if k.get("result") is not None:
            raise RuntimeError("simulated crash after OrderService accepted the order")
        return original(self, *a, **k)

    monkeypatch.setattr(DecisionPipeline, "_decision", crash_on_entry)
    assert w.cycle()["status"] == "FAILED"
    assert orders(w) == [] and decisions(w) == []  # the whole orchestration transaction rolled back
    monkeypatch.setattr(DecisionPipeline, "_decision", original)
    assert w.cycle()["status"] == "SUCCEEDED"
    assert len([o for o in orders(w) if o.source is OrderSource.AGENT]) == 1


def test_repeated_failures_put_the_agent_in_error_and_resume_recovers(world, monkeypatch):
    w = started(world)

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(DecisionPipeline, "process_bar", broken)
    breakout(w)
    w.at("09:35")
    for _ in range(3):
        assert w.cycle()["status"] == "FAILED"
    agent = w.agent()
    assert agent.status is AgentStatus.ERROR and agent.consecutive_failures == 3
    assert events(w, "agent.cycle_failed") and events(w, "agent.error")
    monkeypatch.undo()
    lifecycle.resume(w.db, w.settings, w.ctx(), w.account.user_id, w.clock())
    w.db.commit()
    assert w.agent().status is AgentStatus.RUNNING


def test_phase4_duties_run_even_when_orchestration_fails(world, monkeypatch):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    monkeypatch.setattr(DecisionPipeline, "process_bar", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    assert w.cycle()["status"] == "FAILED"
    assert w.db.scalar(select(Position)).quantity > 0  # T1 processed the fill and committed


def test_an_unexplained_agent_order_is_a_discrepancy(world):
    w = started(world)
    w.db.commit()
    foreign = OrderIntent(client_order_id="AG:E:999:1:1", instrument_id=w.instrument.id, side=OrderSide.BUY,
                          order_type=intent(w.instrument).order_type, quantity=1, source=OrderSource.AGENT,
                          stop_loss_price=D("95"))
    submit(w.db, w.settings, w.clock, w.account, foreign)
    w.cycle()
    agent = w.agent()
    assert agent.status is AgentStatus.ERROR and "RECONCILIATION_FAILED" in agent.status_reason
    with pytest.raises(lifecycle.AgentRefused, match="RECONCILIATION_FAILED"):
        lifecycle.resume(w.db, w.settings, w.ctx(), w.account.user_id, w.clock())
    w.db.rollback()


def test_stop_never_liquidates_and_phase4_monitoring_continues(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    lifecycle.stop(w.db, w.account.user_id, w.clock())
    w.db.commit()
    qty = w.db.scalar(select(Position)).quantity
    assert qty > 0 and len(orders(w)) == 1  # nothing liquidated
    # the stop level (99) is crossed: the STOPPED agent's worker still runs Phase 4 protective exits (D10)
    w.bar("09:45", "100", "100.2", "98.5", "99.5")
    w.at("09:50")
    n = len(decisions(w))
    assert w.cycle()["status"] == "SUCCEEDED"
    assert len(decisions(w)) == n  # no strategy evaluation while stopped
    exits = [o for o in orders(w) if o.source is OrderSource.RISK_EXIT]
    assert len(exits) == 1 and exits[0].side is OrderSide.SELL


def test_watermark_prevents_reprocessing_after_restart(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    n = w.db.scalar(select(func.count()).select_from(AgentDecision))
    # a "restarted" worker with a different identity sees the same state
    w.cycle(worker="another-host:42")
    assert w.db.scalar(select(func.count()).select_from(AgentDecision)) == n


def test_database_guards_reject_tampering(world):
    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    from sqlalchemy.exc import DBAPIError

    for stmt in (update(AgentDecision).values(outcome="NO_SIGNAL"), update(AgentEvent).values(event_type="x")):
        with pytest.raises(DBAPIError, match="append-only"):
            w.db.execute(stmt)
        w.db.rollback()
    with pytest.raises(DBAPIError):
        w.db.execute(text("DELETE FROM agent_instances"))
    w.db.rollback()
    lifecycle.stop(w.db, w.account.user_id, w.clock())
    w.db.commit()
    with pytest.raises(DBAPIError, match="invalid agent status transition"):
        w.db.execute(text("UPDATE agent_instances SET status = 'PAUSED', mode = 'EXECUTION'"))
    w.db.rollback()
    with pytest.raises(DBAPIError, match="finished agent cycle"):
        w.db.execute(text("UPDATE agent_cycles SET summary = '{}'"))
    w.db.rollback()


@pytest.mark.parametrize("strategy_id", ["EMA_CROSSOVER", "ORB", "RSI_BOLLINGER", "SUPERTREND", "VWAP"])
def test_every_approved_strategy_runs_through_the_agent(db, engine, test_settings, strategy_id):
    import math

    from tests.integration.agent_support import DAY, IST, ist

    w = World(db, engine, test_settings, execution=False, day=NEXT_DAY)
    for day, phase in ((DAY, 0), (NEXT_DAY, 11)):
        for i in range(75 if day == DAY else 40):
            c = 100 + 3 * math.sin((i + phase) / 6) + 0.02 * i
            o = c - 0.3 * math.cos((i + phase) / 4)
            hhmm = (ist(day, "09:15") + timedelta(minutes=5 * i)).astimezone(IST).strftime("%H:%M")
            w.bar(hhmm, f"{o:.2f}", f"{max(o, c) + 0.4:.2f}", f"{min(o, c) - 0.4:.2f}", f"{c:.2f}", day)
    w.at("09:15", NEXT_DAY, seconds=0)
    w.configure({"mode": "PINNED", "candidates": [{"config_id": "c", "strategy_id": strategy_id, "params": {}}]},
                warmup_sessions=1)
    w.start()
    w.db.execute(update(AgentInstrumentState).values(last_bar_start=None))  # evaluate the whole session
    w.db.commit()
    w.at("12:35", NEXT_DAY, seconds=0)
    assert w.cycle()["status"] == "SUCCEEDED"
    rows = decisions(w)
    assert len(rows) == 40 and {r.strategy_id for r in rows if r.strategy_id} == {strategy_id}
    state = w.db.scalar(select(AgentInstrumentState))
    assert state.replay_from_session == DAY and state.status is AgentInstrumentStatus.OK
    assert orders(w) == []  # SIGNALS_ONLY


def test_an_instrument_disabled_after_configuration_is_contained(world):
    from app.db.enums import TradingStatus
    from app.db.models import Instrument

    w = started(world)
    w.db.execute(update(Instrument).values(trading_status=TradingStatus.DISABLED, disabled_reason="suspended (test)"))
    w.db.commit()
    breakout(w)
    w.at("09:35")
    assert w.cycle()["status"] == "SUCCEEDED"
    state = w.db.scalar(select(AgentInstrumentState))
    assert state.status is AgentInstrumentStatus.DATA_ERROR and "disabled" in state.status_detail
    assert orders(w) == [] and events(w, "agent.instrument_data_error")


def test_an_owed_exit_on_an_instrument_dropped_by_a_new_version_still_expires(world):
    from tests.integration.trading_support import make_instrument

    w = started(world)
    breakout(w)
    w.at("09:35")
    w.cycle()
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.bar("09:40", "102.3", "102.5", "102.1", "102.4")
    w.at("09:45")
    w.cycle()
    w.flat_bars("09:45", "15:10")
    w.at("15:40")  # stale: the square-off is owed but cannot be submitted
    w.cycle()
    other = make_instrument(w.db, "OTHERCO")
    lifecycle.pause(w.db, w.account.user_id, w.clock())
    w.db.commit()
    _agent, version, _ = w.configure(instrument_ids=[other.id])
    assert version.version == 2 and version.effective_from_session == NEXT_DAY
    lifecycle.resume(w.db, w.settings, w.ctx(), w.account.user_id, w.clock())
    w.db.commit()
    w.at("09:20", NEXT_DAY)
    assert w.cycle()["status"] == "SUCCEEDED"
    state = w.db.scalar(select(AgentInstrumentState).where(AgentInstrumentState.instrument_id == w.instrument.id))
    assert state.owed_exit_since is None and events(w, "agent.owed_exit_expired_session_end")
    assert [o.side for o in orders(w) if o.source is OrderSource.AGENT] == [OrderSide.BUY]
