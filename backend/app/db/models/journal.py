"""Phase 6 tables: the trading journal and stored trade explanations (migration 0013).

All are user-owned. Neither is a source of truth for trading or accounting: orders, fills, positions, the cash ledger,
risk decisions and agent decisions stay authoritative and are only referenced (foreign keys). A journal entry stores a
classification label (`outcome`) and identifiers, never amounts; amounts are read from the authoritative rows when an
entry is shown. Guards (migration 0013):
  * an AUTO entry's system columns (type, source, links, system facts, title, outcome, times) never change and an AUTO
    entry is never deleted; the user's notes on it remain editable. A MANUAL entry is the user's own and may be edited
    or deleted;
  * one AUTO entry per (user, source key): repeated synchronisation never duplicates an entry;
  * every linked record (order, fill, position, agent decision/event/configuration, backtest run) and every tag must
    belong to the entry's user (trigger);
  * stored explanations are append-only and may reference only their own user's decision, order or event.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UserOwnedMixin
from app.db.enums import ExplanationSubject, JournalEntryType, JournalSourceEvent, NarrativeStatus, TradeOutcome
from app.db.models.market import enum_type as _enum

NOTE_COLUMNS = (
    "notes", "entry_thesis", "exit_thesis", "market_observations", "went_well", "went_wrong", "lessons", "emotional_notes",
)

# Full-text search over the title (weight A) and every note (weight B). The two-argument to_tsvector is immutable, so
# the column can be generated and stored; PostgreSQL's built-in text search needs no extension.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, "
    + " || ' ' || ".join(f"coalesce({c}, '')" for c in NOTE_COLUMNS)
    + "), 'B')"
)


def _ts(nullable: bool = False, default_now: bool = False) -> Mapped:
    if default_now:
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=nullable)
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class JournalEntry(UserOwnedMixin, Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        Index("uq_journal_entries_source_key", "user_id", "source_key", unique=True,
              postgresql_where=text("source_key IS NOT NULL")),
        Index("ix_journal_entries_user_occurred", "user_id", "occurred_at"),
        Index("ix_journal_entries_user_session", "user_id", "session_date"),
        Index("ix_journal_entries_search", "search_vector", postgresql_using="gin"),
        CheckConstraint("(entry_type = 'AUTO') = (source_key IS NOT NULL AND source_event IS NOT NULL)",
                        name="auto_iff_source"),
        CheckConstraint("char_length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating_range"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 1 AND 5", name="confidence_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_type: Mapped[JournalEntryType] = mapped_column(_enum(JournalEntryType, "journal_entry_type"), nullable=False)
    source_event: Mapped[JournalSourceEvent | None] = mapped_column(
        _enum(JournalSourceEvent, "journal_source_event"), nullable=True
    )
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = _ts()
    session_date: Mapped[date] = mapped_column(Date, nullable=False)  # exchange-local (IST) date
    outcome: Mapped[TradeOutcome] = mapped_column(_enum(TradeOutcome, "trade_outcome"), nullable=False, server_default="NONE")
    # links to authoritative records (all optional; ownership enforced by trigger)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True, index=True)
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id", ondelete="RESTRICT"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="RESTRICT"), nullable=True)
    agent_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_decisions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    agent_event_id: Mapped[int | None] = mapped_column(ForeignKey("agent_events.id", ondelete="RESTRICT"), nullable=True)
    config_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=True
    )
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=True
    )
    # identifiers and labels captured from the source record (no amounts); immutable for AUTO entries
    system_facts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # the user's notes (editable on every entry)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_observations: Mapped[str | None] = mapped_column(Text, nullable=True)
    went_well: Mapped[str | None] = mapped_column(Text, nullable=True)
    went_wrong: Mapped[str | None] = mapped_column(Text, nullable=True)
    lessons: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    confidence: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    search_vector: Mapped[str] = mapped_column(TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True), nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JournalTag(UserOwnedMixin, Base):
    __tablename__ = "journal_tags"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_journal_tags_user_name"),
        CheckConstraint("name ~ '^[a-z0-9][a-z0-9_-]{0,39}$'", name="name_normalised"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = _ts(default_now=True)


class JournalEntryTag(UserOwnedMixin, Base):
    __tablename__ = "journal_entry_tags"

    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id", ondelete="RESTRICT"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("journal_tags.id", ondelete="RESTRICT"), primary_key=True, index=True)


class JournalReflection(UserOwnedMixin, Base):
    """The user's reflection on a closed trade (one per entry). Saved only by an explicit user request."""

    __tablename__ = "journal_reflections"
    __table_args__ = (UniqueConstraint("entry_id", name="uq_journal_reflections_entry_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False)
    original_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    followed_strategy_rules: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    risk_limits_respected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exit_followed_strategy: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mistakes: Mapped[str | None] = mapped_column(Text, nullable=True)
    lessons: Mapped[str | None] = mapped_column(Text, nullable=True)
    future_improvements: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TradeExplanation(UserOwnedMixin, Base):
    """A stored explanation: the deterministic facts and evidence used, the rule-based text, and an optional LLM
    narrative with its provider metadata. Append-only; regenerating creates a new row."""

    __tablename__ = "trade_explanations"
    __table_args__ = (
        # A successful explanation of the same facts by the same narrator is stored once (idempotent generation).
        Index(
            "uq_trade_explanations_subject_facts", "user_id", "subject_type", "subject_key", "facts_fingerprint",
            "narrator", unique=True,
            postgresql_where=text("narrative_status IN ('NOT_REQUESTED', 'DISABLED', 'GENERATED')"),
        ),
        Index("ix_trade_explanations_user_created", "user_id", "created_at"),
        CheckConstraint("char_length(facts_fingerprint) = 64", name="facts_fingerprint_sha256"),
        CheckConstraint("(narrative_status = 'GENERATED') = (narrative IS NOT NULL)", name="narrative_iff_generated"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_type: Mapped[ExplanationSubject] = mapped_column(
        _enum(ExplanationSubject, "explanation_subject"), nullable=False
    )
    subject_key: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_decisions.id", ondelete="RESTRICT"), nullable=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    agent_event_id: Mapped[int | None] = mapped_column(ForeignKey("agent_events.id", ondelete="RESTRICT"), nullable=True)
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    facts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    facts_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(16), nullable=False)
    deterministic_text: Mapped[str] = mapped_column(Text, nullable=False)
    narrator: Mapped[str] = mapped_column(String(160), nullable=False)  # "rule_based" or "<provider>:<model>"
    narrative_status: Mapped[NarrativeStatus] = mapped_column(_enum(NarrativeStatus, "narrative_status"), nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_caveats: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    narrative_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _ts(default_now=True)
