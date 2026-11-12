"""Phase 6 on PostgreSQL: explanations and automatic journal entries built from real Phase 4/5 records.

The records come from the real agent cycle, OrderService, risk engine and Mock Exchange (synthetic fixture data,
execution enabled only in these tests). Nothing here asserts that live or end-to-end paper execution was validated.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.enums import (
    ExplanationSubject,
    JournalEntryType,
    JournalSourceEvent as S,
    NarrativeStatus,
    OrderStatus,
    TradeOutcome,
)
from app.db.models import (
    AgentDecision,
    CashLedgerEntry,
    Fill,
    JournalEntry,
    Order,
    Position,
    RiskDecision,
    RiskSettings,
    RiskState,
    TradeExplanation,
)
from app.exchange.portfolio import daily_pnl
from app.explanations import facts as F
from app.explanations.service import ExplanationService
from app.explanations.trades import episodes
from app.journal import sync as journal_sync
from app.journal.service import JournalError, JournalService
from app.llm.narrator import Narrator, ungrounded_numbers
from tests.integration.agent_support import DAY, World
from tests.integration.phase6_support import FakeProvider, entry_decision, orders, round_trip
from tests.integration.trading_support import (
    Clock,
    configure_risk,
    enabled_settings,
    intent,
    make_account,
    make_candle,
    make_instrument,
    make_provider,
    next_bar,
    process,
    submit,
)

D = Decimal


@pytest.fixture
def traded(db, engine, test_settings):
    return round_trip(World(db, engine, test_settings))


def grounded(preview) -> None:
    """The rule-based text states no number that is not in the verified facts."""
    assert ungrounded_numbers(preview.text, json.dumps(preview.facts, sort_keys=True)) == []


def snapshot(db) -> tuple:
    """Authoritative trading state: Phase 6 must never change any of it."""
    db.expire_all()
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in
                 (Order, Fill, Position, CashLedgerEntry, RiskDecision, RiskSettings, RiskState, AgentDecision)) + (
        tuple(db.execute(select(Position.quantity, Position.realized_pnl, Position.fees_paid))),
        tuple(db.execute(select(Order.id, Order.status, Order.filled_quantity))),
    )


# ---- trades and explanations -------------------------------------------------------------------------------------


def test_the_round_trip_is_one_closed_trade_built_from_stored_values(traded):
    w = traded
    [trade] = episodes(w.db, w.account.user_id)
    fills = list(w.db.scalars(select(Fill).order_by(Fill.id)))
    assert trade.closed and trade.fill_ids == [f.id for f in fills]
    assert trade.realized_pnl == sum(f.realized_pnl for f in fills)
    assert trade.fees == sum(f.fee for f in fills)
    assert trade.outcome.value in ("WIN", "LOSS", "BREAKEVEN")
    assert len(orders(w)) == 2  # two orders, one trade


def test_strategy_signal_explanation_uses_the_stored_signal_candle_and_configuration(traded):
    w = traded
    d = entry_decision(w)
    svc = ExplanationService(w.db, w.settings, w.clock)
    p = svc.preview(w.account.user_id, ExplanationSubject.AGENT_DECISION, str(d.id))
    layers = dict(p.sections)
    assert "ORB_BREAKOUT" in layers["Strategy signal"] and "entry signal" in layers["Strategy signal"]
    assert "102" in layers["Strategy signal"] and "101" in layers["Strategy signal"]  # stored close and range high
    assert "range 15 minutes" in layers["Strategy signal"]  # the configured (default) parameter
    assert "2026-01-05 09:30 IST" in layers["Strategy signal"]
    assert "ENTRY_SUBMITTED" in layers["Agent decision"] and "Risk decision" in layers and "Order" in layers
    assert p.facts["strategy_signal"]["indicator_values"] == d.values
    ev = p.evidence
    assert ev["candle_id"] == d.candle_id and ev["observation_version"] == 1 and ev["config_version"] == 1
    assert ev["price_basis"] == "UNVERIFIED_PROVIDER_NATIVE" and ev["provider_code"] == "fixture"
    assert ev["order_id"] == str(d.order_id) and ev["risk_decision_id"] == d.risk_decision_id and len(ev["fill_ids"]) == 1
    assert ev["selection_decision_id"] is not None and p.facts["selection"]["selected_config_id"] == "orb-default"
    grounded(p)


def test_order_explanation_copies_fills_fees_and_trade_outcome(traded):
    w = traded
    sell = orders(w)[1]
    p = ExplanationService(w.db, w.settings, w.clock).preview(w.account.user_id, ExplanationSubject.ORDER, str(sell.id))
    fill = w.db.scalar(select(Fill).where(Fill.order_id == sell.id))
    [f] = p.facts["execution"]["fills"]
    assert f["executed_price"] == str(fill.price) and f["fee"] == str(fill.fee) and f["realized_pnl"] == str(fill.realized_pnl)
    assert f["fee_breakdown"] == fill.fee_breakdown
    trade = p.facts["trade"]
    assert trade["status"] == "CLOSED" and D(trade["realized_pnl"]) == sum(x.realized_pnl for x in w.db.scalars(select(Fill)))
    assert "Outcome" in dict(p.sections) and "simulated fills" in p.text
    assert p.facts["agent_decision"]["outcome"] == "EXIT_SUBMITTED"
    grounded(p)


def test_daily_summary_uses_authoritative_pnl_and_never_counts_orders_as_trades(traded):
    w = traded
    f = F.daily_facts(w.db, w.account.user_id, DAY).facts
    pnl = daily_pnl(w.db, w.account.user_id, DAY)
    assert f["orders"]["total"] == 2 and f["closed_trades"]["total"] == 1 and f["fills"] == 2
    assert D(f["realized"]["realized_pnl_gross"]) == pnl.realized_pnl and D(f["realized"]["fees"]) == pnl.fees
    assert D(f["realized"]["net_realized_pnl"]) == pnl.net_realized_pnl
    assert f["unrealized"]["included"] is False and f["unrealized"]["open_positions_now"] == 0
    # 15:10 square-off -> EXIT_OWED; the 15:15 square-off signal -> EXIT_ALREADY_OWED (one owed exit, two signals)
    assert f["signals"]["entry"] == 1 and f["signals"]["exit"] == 2
    assert f["signals"]["by_outcome"] == {"ENTRY_SUBMITTED": 1, "EXIT_OWED": 1, "EXIT_ALREADY_OWED": 1}
    assert f["by_strategy"]["ORB"]["signals"] == 3 and f["by_strategy"]["ORB"]["closed_trades"] == 1
    sym = f["by_instrument"][str(w.instrument.id)]
    assert sym["symbol"] == "TESTCO" and D(sym["realized_pnl"]) == pnl.realized_pnl
    p = ExplanationService(w.db, w.settings, w.clock).preview(w.account.user_id, ExplanationSubject.DAILY_SUMMARY, DAY.isoformat())
    assert "Orders are not trades" in p.text
    grounded(p)


def test_signals_only_decision_is_explained_as_not_submitted(db, engine, test_settings):
    w = World(db, engine, test_settings, execution=False)
    w.opening_range()
    w.at("09:30")
    w.configure()
    w.start()
    w.bar("09:30", "100.5", "102.5", "100.4", "102")
    w.at("09:35")
    w.cycle()
    d = entry_decision(w)
    assert d.outcome.value == "SIGNALS_ONLY_NOT_SUBMITTED"
    p = ExplanationService(w.db, w.settings, w.clock).preview(w.account.user_id, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert "SIGNALS_ONLY" in dict(p.sections)["Agent decision"] and "no order was submitted" in p.text
    assert p.facts["order"] is None and "no order exists" in p.text
    grounded(p)


def test_entry_suppression_is_explained_with_its_recorded_reason(db, engine, test_settings):
    w = World(db, engine, test_settings)
    w.opening_range()
    w.at("09:30")
    w.configure()
    w.start()
    w.bar("09:30", "100.5", "102.5", "100.4", "102")
    w.bar("09:35", "102.1", "102.4", "101.9", "102.2")
    w.at("09:40")  # the cycle runs only after the next bar: the 09:30 signal is late (D5)
    w.cycle()
    d = entry_decision(w)
    assert d.outcome.value == "SUPPRESSED_LATE_SIGNAL"
    p = ExplanationService(w.db, w.settings, w.clock).preview(w.account.user_id, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert "late entry signals are never acted on" in p.text and "latest eligible bar" in p.text
    grounded(p)


@pytest.fixture
def env(db, test_settings):
    provider, inst, account = make_provider(db), make_instrument(db), make_account(db)
    configure_risk(db, account)
    return dict(db=db, provider=provider, inst=inst, account=account, settings=enabled_settings(test_settings), clock=Clock())


def test_partial_fill_cancellation_and_risk_rejection_are_explained_and_journaled(env, db):
    s = enabled_settings(env["settings"], exchange_max_volume_participation=D("0.01"))
    make_candle(db, env["provider"], env["inst"], "100")
    partial = submit(db, s, env["clock"], env["account"], intent(env["inst"], qty=25)).order
    next_bar(db, env["provider"], env["inst"], env["clock"], "100", volume=1000)
    process(db, s, env["clock"], env["account"])
    db.refresh(partial)
    assert partial.status is OrderStatus.CANCELLED and partial.filled_quantity == 10

    poor = make_account(db, capital="500.00")
    configure_risk(db, poor, resize_allowed=False)
    rejected = submit(db, s, env["clock"], poor, intent(env["inst"], qty=10)).order
    assert rejected.status is OrderStatus.REJECTED

    svc = ExplanationService(db, s, env["clock"])
    p = svc.preview(env["account"].user_id, ExplanationSubject.ORDER, str(partial.id))
    assert "partial fill: 10 of 25 filled" in p.text and "cancelled" in p.text
    grounded(p)
    p = svc.preview(poor.user_id, ExplanationSubject.ORDER, str(rejected.id))
    assert "REJECTED" in dict(p.sections)["Risk decision"] and "available_cash" in p.text and "never filled" in p.text
    grounded(p)

    today = date(2026, 1, 5)
    result = journal_sync.sync(db, env["account"].user_id, today, today, env["clock"]())
    assert result["created_by_event"] == {"ORDER_CANCELLED": 1, "MANUAL_TRADE": 1, "PARTIAL_FILL": 1}
    dates = set(db.scalars(select(JournalEntry.session_date).where(JournalEntry.user_id == env["account"].user_id)))
    assert dates == {today}  # dated on the trading clock, never by wall-clock processing time
    result = journal_sync.sync(db, poor.user_id, today, today, env["clock"]())
    assert result["created_by_event"] == {"RISK_REJECTION": 1}
    entry = db.scalar(select(JournalEntry).where(JournalEntry.user_id == poor.user_id))
    assert entry.outcome is TradeOutcome.REJECTED and entry.order_id == rejected.id
    assert entry.system_facts["risk_decision_id"] is not None


# ---- automatic journal entries -----------------------------------------------------------------------------------


def test_sync_creates_one_entry_per_record_and_is_idempotent(traded):
    w = traded
    w.at("16:00")
    before = snapshot(w.db)
    first = journal_sync.sync(w.db, w.account.user_id, DAY, DAY, w.clock())
    w.db.commit()
    assert first["created_by_event"] == {"ENTRY_SIGNAL": 1, "EXIT_SIGNAL": 2, "TRADE_CLOSED": 1, "SESSION_SUMMARY": 1}
    for _ in range(2):
        again = journal_sync.sync(w.db, w.account.user_id, DAY, DAY, w.clock())
        w.db.commit()
        assert again["created"] == 0 and again["candidates"] == first["candidates"]
    assert w.db.scalar(select(func.count()).select_from(JournalEntry)) == 5
    closed = w.db.scalar(select(JournalEntry).where(JournalEntry.source_event == S.TRADE_CLOSED))
    [trade] = episodes(w.db, w.account.user_id)
    assert closed.outcome is trade.outcome and closed.fill_id == trade.closing_fill_id
    assert closed.position_id == w.db.scalar(select(Position.id)) and closed.strategy_id == "ORB"
    assert "realized" not in json.dumps(closed.system_facts).replace("realized P&L", "")  # labels, never amounts
    assert snapshot(w.db) == before  # the journal never writes trading or accounting records


def test_no_session_summary_before_the_session_ends(traded):
    w = traded
    w.at("15:25")
    result = journal_sync.sync(w.db, w.account.user_id, DAY, DAY, w.clock())
    assert "SESSION_SUMMARY" not in result["created_by_event"]


def test_automatic_entry_facts_are_immutable_in_the_database(traded):
    w = traded
    w.at("16:00")
    journal_sync.sync(w.db, w.account.user_id, DAY, DAY, w.clock())
    w.db.commit()
    entry = w.db.scalar(select(JournalEntry).where(JournalEntry.source_event == S.TRADE_CLOSED))
    for change in ({"title": "edited"}, {"outcome": "LOSS" if entry.outcome is not TradeOutcome.LOSS else "WIN"},
                   {"system_facts": {}}, {"order_id": None}):
        with pytest.raises(DBAPIError, match="immutable"):
            w.db.execute(update(JournalEntry).where(JournalEntry.id == entry.id).values(**change))
        w.db.rollback()
    with pytest.raises(DBAPIError, match="cannot be deleted"):
        w.db.execute(text("DELETE FROM journal_entries WHERE id = :i"), {"i": entry.id})
    w.db.rollback()
    w.db.execute(update(JournalEntry).where(JournalEntry.id == entry.id).values(notes="my note"))  # notes are editable
    w.db.commit()


def test_database_refuses_links_to_another_users_records(traded, db):
    w = traded
    other = make_account(db)
    order = orders(w)[0]
    with pytest.raises(DBAPIError, match="own user"):
        db.add(JournalEntry(user_id=other.user_id, entry_type=JournalEntryType.MANUAL, title="x",
                            occurred_at=w.clock(), session_date=DAY, order_id=order.id))
        db.flush()
    db.rollback()


def test_manual_entries_validate_links_and_reflections_need_a_closed_trade(traded):
    w = traded
    svc = JournalService(w.db, w.account.user_id)
    sell = orders(w)[1]
    entry = svc.create_manual({"title": "Breakout review", "order_id": sell.id, "notes": "Held to square-off"},
                              ["ORB", "review"], w.clock())
    assert entry.instrument_id == w.instrument.id and entry.outcome.value in ("WIN", "LOSS", "BREAKEVEN")
    assert svc.tags_of([entry.id])[entry.id] == ["orb", "review"]
    reflection, created = svc.save_reflection(entry.id, {"followed_strategy_rules": True, "lessons": "Wait"}, w.clock())
    assert created
    loose = svc.create_manual({"title": "Market thoughts"}, [], w.clock())
    with pytest.raises(JournalError, match="closed trade"):
        svc.save_reflection(loose.id, {"lessons": "x"}, w.clock())
    other = make_account(w.db)
    with pytest.raises(JournalError) as exc:
        JournalService(w.db, other.user_id).create_manual({"title": "x", "order_id": sell.id}, [], w.clock())
    assert exc.value.code == "LINK_NOT_FOUND"
    with pytest.raises(JournalError) as exc:
        svc.create_manual({"title": "x", "order_id": sell.id, "fill_id": w.db.scalar(select(Fill.id).where(
            Fill.order_id == orders(w)[0].id))}, [], w.clock())
    assert exc.value.code == "LINKS_INCONSISTENT"


# ---- stored explanations -------------------------------------------------------------------------------------------


def test_generation_is_idempotent_and_failures_fall_back(traded):
    w = traded
    d = entry_decision(w)
    uid = w.account.user_id
    svc = ExplanationService(w.db, w.settings, w.clock)
    row, created = svc.generate(uid, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert created and row.narrative_status is NarrativeStatus.DISABLED and row.narrator == "rule_based"
    again, created = svc.generate(uid, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert not created and again.id == row.id

    timeout = FakeProvider(error=__import__("app.llm.providers", fromlist=["ProviderError"]).ProviderError("TIMEOUT"))
    svc = ExplanationService(w.db, w.settings.model_copy(update={"llm_max_retries": 0}), w.clock,
                             Narrator(w.settings, timeout))
    row, created = svc.generate(uid, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert created and row.narrative_status is NarrativeStatus.FAILED and row.narrative is None
    assert row.deterministic_text and row.narrative_error == "TIMEOUT"

    ok = FakeProvider()
    svc = ExplanationService(w.db, w.settings, w.clock, Narrator(w.settings, ok))
    row, created = svc.generate(uid, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert created and row.narrative_status is NarrativeStatus.GENERATED and row.narrator == "fake:fake-1"
    row2, created = svc.generate(uid, ExplanationSubject.AGENT_DECISION, str(d.id))
    assert not created and row2.id == row.id and ok.calls == 1
    w.db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        w.db.execute(update(TradeExplanation).where(TradeExplanation.id == row.id).values(narrative="changed"))
    w.db.rollback()


def test_the_hourly_llm_budget_falls_back_to_rule_based(traded):
    w = traded
    s = w.settings.model_copy(update={"llm_max_requests_per_user_per_hour": 1})
    fake = FakeProvider()
    svc = ExplanationService(w.db, s, w.clock, Narrator(s, fake))
    uid = w.account.user_id
    first, _ = svc.generate(uid, ExplanationSubject.DAILY_SUMMARY, DAY.isoformat())
    second, _ = svc.generate(uid, ExplanationSubject.ORDER, str(orders(w)[0].id))
    assert first.narrative_status is NarrativeStatus.GENERATED
    assert second.narrative_status is NarrativeStatus.RATE_LIMITED and second.deterministic_text and fake.calls == 1


def test_a_generated_narrative_changes_no_trading_state(traded):
    w = traded
    before = snapshot(w.db)
    svc = ExplanationService(w.db, w.settings, w.clock, Narrator(w.settings, FakeProvider()))
    for d in w.db.scalars(select(AgentDecision)):
        svc.generate(w.account.user_id, ExplanationSubject.AGENT_DECISION, str(d.id))
    for o in orders(w):
        svc.generate(w.account.user_id, ExplanationSubject.ORDER, str(o.id))
    w.db.commit()
    assert snapshot(w.db) == before
