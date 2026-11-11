"""agent orchestration: agent lifecycle, configuration versions, cycles, decisions, events, instrument state (Phase 5)

Additive; no existing table, column, trigger or migration is touched. Six user-owned tables:
  * agent_instances: one per user (unique user_id); never deleted; identity immutable; status transitions limited to
    STOPPED->RUNNING, RUNNING->PAUSED|STOPPED|ERROR, PAUSED->RUNNING|STOPPED|ERROR, ERROR->RUNNING|STOPPED (trigger).
  * agent_config_versions, agent_decisions, agent_events: append-only (trigger).
  * agent_cycles: never deleted; a finished cycle is frozen; at most one RUNNING cycle per agent (partial unique index).
  * agent_instrument_states: never deleted; identity immutable.
  * Every row's user_id must equal its agent's (insert trigger), and a decision may reference only the same user's
    order and risk decision, so no agent record can point at another user's data.
  * agent_decisions: one evaluation per (agent, instrument, bar), one selection per (agent, instrument, session) and
    one row per (agent, client order id) (partial unique indexes): a candle is never processed twice and an order is
    never submitted twice.
Downgrade is refused while any agent record exists.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TABLES = ("agent_instrument_states", "agent_events", "agent_decisions", "agent_cycles", "agent_config_versions", "agent_instances")
STATUS = ("STOPPED", "RUNNING", "PAUSED", "ERROR")
MODE = ("EXECUTION", "SIGNALS_ONLY")


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _ts(name: str, nullable: bool = False, now: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.text("now()") if now else None, nullable=nullable)


def _fk(table: str, col: str, ref: str) -> sa.ForeignKeyConstraint:
    ref_table = ref.split(".")[0]
    return sa.ForeignKeyConstraint([col], [ref], name=op.f(f"fk_{table}_{col}_{ref_table}"), ondelete="RESTRICT")


def _owned(table: str) -> list:
    return [sa.Column("user_id", sa.UUID(), nullable=False), _fk(table, "user_id", "users.id"),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}"))]


def upgrade() -> None:
    t = "agent_instances"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("status", _enum("agent_status", *STATUS), server_default="STOPPED", nullable=False),
        sa.Column("mode", _enum("agent_mode", *MODE), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        _ts("status_changed_at"),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        _ts("last_heartbeat_at", nullable=True),
        sa.Column("last_cycle_id", sa.BigInteger(), nullable=True),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        sa.CheckConstraint("status NOT IN ('RUNNING', 'PAUSED') OR mode IS NOT NULL", name=op.f(f"ck_{t}_active_has_mode")),
        sa.CheckConstraint("consecutive_failures >= 0", name=op.f(f"ck_{t}_failures_non_negative")),
        _fk(t, "account_id", "virtual_accounts.id"),
        sa.UniqueConstraint("user_id", name="uq_agent_instances_user_id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])

    t = "agent_config_versions"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("selection_mode", sa.String(length=32), nullable=False),
        sa.Column("instrument_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warmup_sessions", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("effective_from_session", sa.Date(), nullable=False),
        _ts("created_at"),
        sa.CheckConstraint("version >= 1", name=op.f(f"ck_{t}_version_positive")),
        sa.CheckConstraint("selection_mode IN ('REGIME_PRIORITY', 'PINNED')", name=op.f(f"ck_{t}_selection_mode_valid")),
        sa.CheckConstraint("warmup_sessions >= 0 AND warmup_sessions <= 20", name=op.f(f"ck_{t}_warmup_sessions_range")),
        _fk(t, "agent_id", "agent_instances.id"),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_config_versions_agent_version"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_agent_id"), t, ["agent_id"])
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])

    t = "agent_cycles"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("status", _enum("agent_cycle_status", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("agent_status", _enum("agent_status", *STATUS), nullable=False),
        sa.Column("mode", _enum("agent_mode", *MODE), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name=op.f(f"ck_{t}_finished_iff_not_running")),
        _fk(t, "agent_id", "agent_instances.id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_agent_id"), t, ["agent_id"])
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index("uq_agent_cycles_one_running", t, ["agent_id"], unique=True, postgresql_where=sa.text("status = 'RUNNING'"))

    t = "agent_decisions"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("config_version_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("kind", _enum("agent_decision_kind", "SELECTION", "EVALUATION", "OWED_EXIT"), nullable=False),
        _ts("bar_start", nullable=True),
        sa.Column("candle_id", sa.BigInteger(), nullable=True),
        sa.Column("observation_version", sa.Integer(), nullable=True),
        sa.Column("provider_code", sa.String(length=32), nullable=True),
        sa.Column("price_basis", sa.String(length=32), nullable=True),
        sa.Column("config_id", sa.String(length=64), nullable=True),
        sa.Column("strategy_id", sa.String(length=32), nullable=True),
        sa.Column("strategy_version", sa.String(length=16), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.String(length=48), nullable=True),
        sa.Column("outcome", _enum(
            "agent_decision_outcome", "SELECTED", "NO_SELECTION", "NO_SIGNAL", "ENTRY_SUBMITTED", "EXIT_SUBMITTED",
            "EXIT_OWED", "EXIT_ALREADY_OWED", "SIGNALS_ONLY_NOT_SUBMITTED", "SUPPRESSED_PAUSED", "SUPPRESSED_KILL_SWITCH",
            "SUPPRESSED_INSTRUMENT_BLOCKED", "SUPPRESSED_POSITION_OR_ORDER", "SUPPRESSED_LATE_SIGNAL",
            "SUPPRESSED_PRICE_NOT_ELIGIBLE", "SUPPRESSED_INVALID_LEVELS", "SUPPRESSED_NO_QUANTITY", "SUPPRESSED_NOT_HELD",
        ), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("risk_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("order_status", sa.String(length=32), nullable=True),
        _ts("decided_at"),
        sa.CheckConstraint("(client_order_id IS NULL) = (order_id IS NULL)", name=op.f(f"ck_{t}_order_iff_client_order_id")),
        sa.CheckConstraint("kind = 'SELECTION' OR bar_start IS NOT NULL", name=op.f(f"ck_{t}_evaluation_has_bar")),
        _fk(t, "agent_id", "agent_instances.id"),
        _fk(t, "cycle_id", "agent_cycles.id"),
        _fk(t, "config_version_id", "agent_config_versions.id"),
        _fk(t, "instrument_id", "instruments.id"),
        _fk(t, "candle_id", "candles.id"),
        _fk(t, "order_id", "orders.id"),
        _fk(t, "risk_decision_id", "risk_decisions.id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_cycle_id"), t, ["cycle_id"])
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index("ix_agent_decisions_agent_decided", t, ["agent_id", "decided_at"])
    op.create_index("uq_agent_decisions_one_evaluation_per_bar", t, ["agent_id", "instrument_id", "bar_start"], unique=True,
                    postgresql_where=sa.text("kind = 'EVALUATION'"))
    op.create_index("uq_agent_decisions_one_selection_per_session", t, ["agent_id", "instrument_id", "session_date"],
                    unique=True, postgresql_where=sa.text("kind = 'SELECTION'"))
    op.create_index("uq_agent_decisions_client_order_id", t, ["agent_id", "client_order_id"], unique=True,
                    postgresql_where=sa.text("client_order_id IS NOT NULL"))

    t = "agent_events"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        _ts("occurred_at"),
        sa.CheckConstraint("actor IN ('USER', 'WORKER', 'SYSTEM')", name=op.f(f"ck_{t}_actor_valid")),
        _fk(t, "agent_id", "agent_instances.id"),
        _fk(t, "cycle_id", "agent_cycles.id"),
        _fk(t, "instrument_id", "instruments.id"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])
    op.create_index("ix_agent_events_agent_occurred", t, ["agent_id", "occurred_at"])

    t = "agent_instrument_states"
    op.create_table(
        t,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("status", _enum("agent_instrument_status", "OK", "MANUAL_INTERVENTION", "ORPHANED_OVERNIGHT", "DATA_ERROR"),
                  server_default="OK", nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("status_session", sa.Date(), nullable=True),
        _ts("last_bar_start", nullable=True),
        sa.Column("last_candle_id", sa.BigInteger(), nullable=True),
        sa.Column("replay_session", sa.Date(), nullable=True),
        sa.Column("replay_from_session", sa.Date(), nullable=True),
        _ts("owed_exit_since", nullable=True),
        sa.Column("owed_exit_session", sa.Date(), nullable=True),
        sa.Column("owed_exit_reason", sa.String(length=48), nullable=True),
        _ts("updated_at", now=True),
        sa.CheckConstraint("(owed_exit_since IS NULL) = (owed_exit_session IS NULL)", name=op.f(f"ck_{t}_owed_exit_complete")),
        _fk(t, "agent_id", "agent_instances.id"),
        _fk(t, "instrument_id", "instruments.id"),
        sa.UniqueConstraint("agent_id", "instrument_id", name="uq_agent_instrument_states_agent_instrument"),
        *_owned(t),
    )
    op.create_index(op.f(f"ix_{t}_user_id"), t, ["user_id"])

    op.execute(
        """
        CREATE FUNCTION agent_instances_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agents cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (NEW.id, NEW.user_id, NEW.account_id, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.user_id, OLD.account_id, OLD.created_at) THEN
                RAISE EXCEPTION 'agent identity columns are immutable' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                   (OLD.status = 'STOPPED' AND NEW.status = 'RUNNING')
                OR (OLD.status = 'RUNNING' AND NEW.status IN ('PAUSED', 'STOPPED', 'ERROR'))
                OR (OLD.status = 'PAUSED' AND NEW.status IN ('RUNNING', 'STOPPED', 'ERROR'))
                OR (OLD.status = 'ERROR' AND NEW.status IN ('RUNNING', 'STOPPED'))) THEN
                RAISE EXCEPTION 'invalid agent status transition % -> %', OLD.status, NEW.status
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION agent_append_only_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION agent_cycles_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent cycles cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'a finished agent cycle cannot change' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (NEW.id, NEW.user_id, NEW.agent_id, NEW.worker_id, NEW.started_at, NEW.session_date)
               IS DISTINCT FROM (OLD.id, OLD.user_id, OLD.agent_id, OLD.worker_id, OLD.started_at, OLD.session_date) THEN
                RAISE EXCEPTION 'agent cycle identity columns are immutable' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION agent_instrument_states_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent instrument states cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (NEW.id, NEW.user_id, NEW.agent_id, NEW.instrument_id)
               IS DISTINCT FROM (OLD.id, OLD.user_id, OLD.agent_id, OLD.instrument_id) THEN
                RAISE EXCEPTION 'agent instrument state identity columns are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION agent_owner_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM agent_instances a WHERE a.id = NEW.agent_id AND a.user_id = NEW.user_id) THEN
                RAISE EXCEPTION '% row must belong to its agent''s user', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_TABLE_NAME = 'agent_decisions' THEN
                IF NEW.order_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM orders o WHERE o.id = NEW.order_id AND o.user_id = NEW.user_id
                        AND o.client_order_id = NEW.client_order_id) THEN
                    RAISE EXCEPTION 'an agent decision must reference its own user''s order by its client order id'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.risk_decision_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM risk_decisions r WHERE r.id = NEW.risk_decision_id AND r.user_id = NEW.user_id) THEN
                    RAISE EXCEPTION 'an agent decision must reference its own user''s risk decision'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("CREATE TRIGGER trg_agent_instances_guard BEFORE UPDATE OR DELETE ON agent_instances "
               "FOR EACH ROW EXECUTE FUNCTION agent_instances_guard()")
    for table in ("agent_config_versions", "agent_decisions", "agent_events"):
        op.execute(f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION agent_append_only_guard()")
    op.execute("CREATE TRIGGER trg_agent_cycles_guard BEFORE UPDATE OR DELETE ON agent_cycles "
               "FOR EACH ROW EXECUTE FUNCTION agent_cycles_guard()")
    op.execute("CREATE TRIGGER trg_agent_instrument_states_guard BEFORE UPDATE OR DELETE ON agent_instrument_states "
               "FOR EACH ROW EXECUTE FUNCTION agent_instrument_states_guard()")
    for table in ("agent_config_versions", "agent_cycles", "agent_decisions", "agent_events", "agent_instrument_states"):
        op.execute(f"CREATE TRIGGER trg_{table}_owner BEFORE INSERT ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION agent_owner_guard()")


def downgrade() -> None:
    conn = op.get_bind()
    for table in TABLES:
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError(f"cannot downgrade 0012: records would be discarded from {table}")
    for table in ("agent_config_versions", "agent_cycles", "agent_decisions", "agent_events", "agent_instrument_states"):
        op.execute(f"DROP TRIGGER trg_{table}_owner ON {table}")
    for table in ("agent_config_versions", "agent_decisions", "agent_events"):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP TRIGGER trg_agent_instrument_states_guard ON agent_instrument_states")
    op.execute("DROP TRIGGER trg_agent_cycles_guard ON agent_cycles")
    op.execute("DROP TRIGGER trg_agent_instances_guard ON agent_instances")
    for fn in ("agent_owner_guard", "agent_instrument_states_guard", "agent_cycles_guard", "agent_append_only_guard",
               "agent_instances_guard"):
        op.execute(f"DROP FUNCTION {fn}()")
    for table in TABLES:
        op.drop_table(table)
