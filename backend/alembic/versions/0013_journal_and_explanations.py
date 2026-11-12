"""journal and explanations: journal entries, tags, reflections, stored trade explanations (Phase 6)

Additive; no existing table, column, trigger or migration is touched. Five user-owned tables:
  * journal_entries: MANUAL entries (the user's) and AUTO entries (one per authoritative trading record, unique
    (user_id, source_key)). An AUTO entry is never deleted and its system columns never change; notes stay editable
    (trigger). Every linked order, fill, position, agent decision, event, configuration version and backtest run must
    belong to the entry's user (trigger). A generated, stored tsvector column and a GIN index serve full-text search
    (built-in PostgreSQL text search; no extension).
  * journal_tags, journal_entry_tags: per-user tags; an association must join an entry and a tag of its own user.
  * journal_reflections: one per entry, the entry's user only.
  * trade_explanations: append-only (trigger); may reference only its own user's decision, order and event; one
    successful explanation per (user, subject, facts fingerprint, narrator) (partial unique index).
Downgrade is refused while any journal or explanation record exists.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

TABLES = ("trade_explanations", "journal_reflections", "journal_entry_tags", "journal_tags", "journal_entries")
SOURCE_EVENTS = (
    "TRADE_CLOSED", "ENTRY_SIGNAL", "EXIT_SIGNAL", "RISK_REJECTION", "ORDER_REJECTED", "ORDER_CANCELLED", "PARTIAL_FILL",
    "PROTECTIVE_EXIT", "AGENT_INTERVENTION", "MANUAL_TRADE", "SESSION_SUMMARY",
)
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "OPEN", "REJECTED", "CANCELLED", "UNKNOWN", "NONE")
NARRATIVE = ("NOT_REQUESTED", "DISABLED", "GENERATED", "FAILED", "INVALID_OUTPUT", "RATE_LIMITED")
SUBJECTS = ("AGENT_DECISION", "ORDER", "AGENT_EVENT", "DAILY_SUMMARY", "JOURNAL_INSIGHTS")
NOTES = ("notes", "entry_thesis", "exit_thesis", "market_observations", "went_well", "went_wrong", "lessons",
         "emotional_notes")
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, "
    + " || ' ' || ".join(f"coalesce({c}, '')" for c in NOTES)
    + "), 'B')"
)
AUTO_IMMUTABLE = ("title", "occurred_at", "session_date", "outcome", "instrument_id", "strategy_id", "order_id", "fill_id",
                  "position_id", "agent_decision_id", "agent_event_id", "config_version_id", "backtest_run_id",
                  "system_facts")
# (column, referenced table) checked for ownership on journal entries
ENTRY_LINKS = (("order_id", "orders"), ("fill_id", "fills"), ("position_id", "positions"),
               ("agent_decision_id", "agent_decisions"), ("agent_event_id", "agent_events"),
               ("config_version_id", "agent_config_versions"), ("backtest_run_id", "backtest_runs"))
EXPLANATION_LINKS = (("agent_decision_id", "agent_decisions"), ("order_id", "orders"), ("agent_event_id", "agent_events"))


def _enum(name: str, *values: str, length: int = 32) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _ts(name: str, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()") if now else None, nullable=nullable)


def _fk(table: str, col: str, ref: str) -> sa.ForeignKeyConstraint:
    ref_table = ref.split(".")[0]
    return sa.ForeignKeyConstraint([col], [ref], name=op.f(f"fk_{table}_{col}_{ref_table}"), ondelete="RESTRICT")


def _owned(table: str, pk: tuple[str, ...] = ("id",)) -> list:
    return [sa.Column("user_id", sa.UUID(), nullable=False), _fk(table, "user_id", "users.id"),
            sa.PrimaryKeyConstraint(*pk, name=op.f(f"pk_{table}"))]


def _ownership_checks(links) -> str:
    return "\n".join(
        f"""
            IF NEW.{col} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {ref} r WHERE r.id = NEW.{col} AND r.user_id = NEW.user_id) THEN
                RAISE EXCEPTION '% must reference its own user''s {ref} row', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;"""
        for col, ref in links
    )


def upgrade() -> None:
    t = "journal_entries"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entry_type", _enum("journal_entry_type", "MANUAL", "AUTO"), nullable=False),
        sa.Column("source_event", _enum("journal_source_event", *SOURCE_EVENTS), nullable=True),
        sa.Column("source_key", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        _ts("occurred_at"),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("outcome", _enum("trade_outcome", *OUTCOMES), server_default="NONE", nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.String(length=32), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("fill_id", sa.BigInteger(), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("agent_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("agent_event_id", sa.BigInteger(), nullable=True),
        sa.Column("config_version_id", sa.BigInteger(), nullable=True),
        sa.Column("backtest_run_id", sa.UUID(), nullable=True),
        sa.Column("system_facts", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        *[sa.Column(c, sa.Text(), nullable=True) for c in NOTES],
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), sa.Computed(SEARCH_VECTOR_SQL, persisted=True), nullable=True),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        sa.CheckConstraint("(entry_type = 'AUTO') = (source_key IS NOT NULL AND source_event IS NOT NULL)",
                           name=op.f(f"ck_{t}_auto_iff_source")),
        sa.CheckConstraint("char_length(btrim(title)) > 0", name=op.f(f"ck_{t}_title_not_blank")),
        sa.CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name=op.f(f"ck_{t}_rating_range")),
        sa.CheckConstraint("confidence IS NULL OR confidence BETWEEN 1 AND 5", name=op.f(f"ck_{t}_confidence_range")),
        _fk(t, "instrument_id", "instruments.id"),
        _fk(t, "order_id", "orders.id"),
        _fk(t, "fill_id", "fills.id"),
        _fk(t, "position_id", "positions.id"),
        _fk(t, "agent_decision_id", "agent_decisions.id"),
        _fk(t, "agent_event_id", "agent_events.id"),
        _fk(t, "config_version_id", "agent_config_versions.id"),
        _fk(t, "backtest_run_id", "backtest_runs.id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index(op.f(f"ix_{t}_order_id"), t, ["order_id"])
    op.create_index(op.f(f"ix_{t}_agent_decision_id"), t, ["agent_decision_id"])
    op.create_index("uq_journal_entries_source_key", t, ["user_id", "source_key"], unique=True,
                    postgresql_where=sa.text("source_key IS NOT NULL"))
    op.create_index("ix_journal_entries_user_occurred", t, ["user_id", "occurred_at"])
    op.create_index("ix_journal_entries_user_session", t, ["user_id", "session_date"])
    op.create_index("ix_journal_entries_search", t, ["search_vector"], postgresql_using="gin")

    t = "journal_tags"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        _ts("created_at", now=True),
        sa.CheckConstraint("name ~ '^[a-z0-9][a-z0-9_-]{0,39}$'", name=op.f(f"ck_{t}_name_normalised")),
        sa.UniqueConstraint("user_id", "name", name="uq_journal_tags_user_name"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])

    t = "journal_entry_tags"
    op.create_table(
        t,
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        _fk(t, "entry_id", "journal_entries.id"),
        _fk(t, "tag_id", "journal_tags.id"),
        *_owned(t, ("entry_id", "tag_id")),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index(op.f(f"ix_{t}_tag_id"), t, ["tag_id"])

    t = "journal_reflections"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("original_thesis", sa.Text(), nullable=True),
        sa.Column("followed_strategy_rules", sa.Boolean(), nullable=True),
        sa.Column("risk_limits_respected", sa.Boolean(), nullable=True),
        sa.Column("exit_followed_strategy", sa.Boolean(), nullable=True),
        sa.Column("mistakes", sa.Text(), nullable=True),
        sa.Column("lessons", sa.Text(), nullable=True),
        sa.Column("future_improvements", sa.Text(), nullable=True),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _fk(t, "entry_id", "journal_entries.id"),
        sa.UniqueConstraint("entry_id", name="uq_journal_reflections_entry_id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])

    t = "trade_explanations"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_type", _enum("explanation_subject", *SUBJECTS), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=False),
        sa.Column("agent_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("agent_event_id", sa.BigInteger(), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("facts_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("generator_version", sa.String(length=16), nullable=False),
        sa.Column("deterministic_text", sa.Text(), nullable=False),
        sa.Column("narrator", sa.String(length=160), nullable=False),
        sa.Column("narrative_status", _enum("narrative_status", *NARRATIVE), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("narrative_caveats", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("narrative_error", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _ts("created_at", now=True),
        sa.CheckConstraint("char_length(facts_fingerprint) = 64", name=op.f(f"ck_{t}_facts_fingerprint_sha256")),
        sa.CheckConstraint("(narrative_status = 'GENERATED') = (narrative IS NOT NULL)",
                           name=op.f(f"ck_{t}_narrative_iff_generated")),
        _fk(t, "agent_decision_id", "agent_decisions.id"),
        _fk(t, "order_id", "orders.id"),
        _fk(t, "agent_event_id", "agent_events.id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index("ix_trade_explanations_user_created", t, ["user_id", "created_at"])
    op.create_index(
        "uq_trade_explanations_subject_facts", t,
        ["user_id", "subject_type", "subject_key", "facts_fingerprint", "narrator"], unique=True,
        postgresql_where=sa.text("narrative_status IN ('NOT_REQUESTED', 'DISABLED', 'GENERATED')"),
    )

    immutable = ", ".join(f"NEW.{c}" for c in AUTO_IMMUTABLE)
    immutable_old = ", ".join(f"OLD.{c}" for c in AUTO_IMMUTABLE)
    op.execute(
        f"""
        CREATE FUNCTION journal_entries_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.entry_type = 'AUTO' THEN
                    RAISE EXCEPTION 'automatic journal entries cannot be deleted'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF (NEW.id, NEW.user_id, NEW.entry_type, NEW.source_event, NEW.source_key, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.user_id, OLD.entry_type, OLD.source_event, OLD.source_key, OLD.created_at) THEN
                RAISE EXCEPTION 'journal entry identity columns are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.entry_type = 'AUTO' AND ({immutable}) IS DISTINCT FROM ({immutable_old}) THEN
                RAISE EXCEPTION 'the system facts of an automatic journal entry are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION journal_entries_owner_guard() RETURNS trigger AS $$
        BEGIN
            {_ownership_checks(ENTRY_LINKS)}
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION journal_child_owner_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM journal_entries e WHERE e.id = NEW.entry_id AND e.user_id = NEW.user_id) THEN
                RAISE EXCEPTION '% row must belong to its journal entry''s user', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            -- nested: PL/pgSQL does not short-circuit AND, and journal_reflections has no tag_id
            IF TG_TABLE_NAME = 'journal_entry_tags' THEN
                IF NOT EXISTS (SELECT 1 FROM journal_tags g WHERE g.id = NEW.tag_id AND g.user_id = NEW.user_id) THEN
                    RAISE EXCEPTION 'a journal entry may only carry its own user''s tags'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION trade_explanations_owner_guard() RETURNS trigger AS $$
        BEGIN
            {_ownership_checks(EXPLANATION_LINKS)}
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION trade_explanations_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'trade explanations are append-only' USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("CREATE TRIGGER trg_journal_entries_guard BEFORE UPDATE OR DELETE ON journal_entries "
               "FOR EACH ROW EXECUTE FUNCTION journal_entries_guard()")
    op.execute("CREATE TRIGGER trg_journal_entries_owner BEFORE INSERT OR UPDATE ON journal_entries "
               "FOR EACH ROW EXECUTE FUNCTION journal_entries_owner_guard()")
    for table in ("journal_entry_tags", "journal_reflections"):
        op.execute(f"CREATE TRIGGER trg_{table}_owner BEFORE INSERT OR UPDATE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION journal_child_owner_guard()")
    op.execute("CREATE TRIGGER trg_trade_explanations_owner BEFORE INSERT ON trade_explanations "
               "FOR EACH ROW EXECUTE FUNCTION trade_explanations_owner_guard()")
    op.execute("CREATE TRIGGER trg_trade_explanations_append_only BEFORE UPDATE OR DELETE ON trade_explanations "
               "FOR EACH ROW EXECUTE FUNCTION trade_explanations_append_only()")


def downgrade() -> None:
    conn = op.get_bind()
    for table in TABLES:
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError(f"cannot downgrade 0013: records would be discarded from {table}")
    op.execute("DROP TRIGGER trg_trade_explanations_append_only ON trade_explanations")
    op.execute("DROP TRIGGER trg_trade_explanations_owner ON trade_explanations")
    for table in ("journal_entry_tags", "journal_reflections"):
        op.execute(f"DROP TRIGGER trg_{table}_owner ON {table}")
    op.execute("DROP TRIGGER trg_journal_entries_owner ON journal_entries")
    op.execute("DROP TRIGGER trg_journal_entries_guard ON journal_entries")
    for fn in ("trade_explanations_append_only", "trade_explanations_owner_guard", "journal_child_owner_guard",
               "journal_entries_owner_guard", "journal_entries_guard"):
        op.execute(f"DROP FUNCTION {fn}()")
    for table in TABLES:
        op.drop_table(table)
