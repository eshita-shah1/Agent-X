"""Reflection prompts for a closed trade: neutral questions built from the trade's facts (deterministic).

They ask the user to compare what happened with their own thesis and the strategy's rules. They never judge the user,
label behaviour or diagnose anything; the user decides what to write and saves it explicitly.
"""

from __future__ import annotations

from app.db.models import JournalEntry
from app.explanations.service import DRAFT_AUDIT_EVENT  # noqa: F401 (re-exported for the API)
from app.explanations.trades import Episode


def reflection_task() -> str:
    return ("Draft a short, neutral, factual summary of this closed paper trade that the user can use as a starting "
            "point for their own reflection. Describe what happened only; do not judge the person or their decisions.")


def reflection_questions(entry: JournalEntry, trade: Episode, linked: dict) -> list[str]:
    symbol = (linked.get("instrument") or {}).get("symbol") or "this instrument"
    strategy = trade.strategy_id or "your own plan"
    exits = ", ".join(sorted(set(trade.exit_sources))) or "a corporate-action adjustment"
    questions = [
        f"What was your original thesis for the {symbol} trade opened on {trade.opened_at.date().isoformat()}?",
        f"Did the entry follow the rules of {strategy}? Which condition triggered it?",
        "Were your risk limits (risk per trade, position size, stop-loss) respected as configured?",
        f"The position was closed by {exits}. Did the exit follow the strategy or your plan?",
        f"The trade is recorded as {trade.outcome.value}. Which parts of the outcome were within your control, and "
        "which were not?",
        "What, if anything, would you do the same way next time?",
        "What would you change or test next time (in a backtest or on paper) before relying on it?",
    ]
    if entry.entry_thesis:
        questions.insert(1, "Compare your recorded entry thesis with what the market actually did.")
    return questions
