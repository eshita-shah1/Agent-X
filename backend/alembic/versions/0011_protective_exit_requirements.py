"""protective-exit requirements: stale prices never silently drop a stop-loss/take-profit evaluation

Additive; no existing row is touched:
  * protective_exit_requirements: one row per position whose protective exit could not be evaluated because no
    eligible fresh price existed. OPEN until an outcome on fresh data (or the position closing) resolves it. A partial
    unique index allows at most one OPEN row per position. Rows are never deleted and resolved rows are frozen
    (trigger).
Downgrade is refused while any requirement exists.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

T = "protective_exit_requirements"


def upgrade() -> None:
    op.create_table(
        T,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("stale_trigger", sa.String(length=2), nullable=True),
        sa.Column("quote_ref", sa.String(length=64), nullable=True),
        sa.Column("quote_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_loss_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("take_profit_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checks", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("exit_order_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name=op.f(f"ck_{T}_status_valid")),
        sa.CheckConstraint("stale_trigger IS NULL OR stale_trigger IN ('SL', 'TP')", name=op.f(f"ck_{T}_stale_trigger_valid")),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolved_at IS NULL AND resolution IS NULL)"
            " OR (status = 'RESOLVED' AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name=op.f(f"ck_{T}_resolution_matches_status"),
        ),
        sa.CheckConstraint("checks >= 1", name=op.f(f"ck_{T}_checks_positive")),
        sa.ForeignKeyConstraint(["account_id"], ["virtual_accounts.id"], name=op.f(f"fk_{T}_account_id_virtual_accounts"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], name=op.f(f"fk_{T}_position_id_positions"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name=op.f(f"fk_{T}_instrument_id_instruments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["exit_order_id"], ["orders.id"], name=op.f(f"fk_{T}_exit_order_id_orders"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f(f"fk_{T}_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{T}")),
    )
    op.create_index(op.f(f"ix_{T}_position_id"), T, ["position_id"])
    op.create_index(op.f(f"ix_{T}_user_id"), T, ["user_id"])
    op.create_index(
        "uq_protective_exit_requirements_open_per_position", T, ["position_id"], unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.execute(
        """
        CREATE FUNCTION protective_exit_requirements_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'protective-exit requirements cannot be deleted'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status = 'RESOLVED' THEN
                RAISE EXCEPTION 'a resolved protective-exit requirement cannot change'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (NEW.id, NEW.user_id, NEW.account_id, NEW.position_id, NEW.instrument_id, NEW.reason_code,
                NEW.detected_at, NEW.created_at, NEW.stop_loss_price, NEW.take_profit_price)
               IS DISTINCT FROM
               (OLD.id, OLD.user_id, OLD.account_id, OLD.position_id, OLD.instrument_id, OLD.reason_code,
                OLD.detected_at, OLD.created_at, OLD.stop_loss_price, OLD.take_profit_price) THEN
                RAISE EXCEPTION 'protective-exit requirement identity columns are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            -- an observed crossing is never forgotten while the requirement is open
            IF OLD.stale_trigger IS NOT NULL AND NEW.stale_trigger IS DISTINCT FROM OLD.stale_trigger THEN
                RAISE EXCEPTION 'a recorded stale trigger cannot be cleared'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{T}_guard BEFORE UPDATE OR DELETE ON {T} "
        "FOR EACH ROW EXECUTE FUNCTION protective_exit_requirements_guard()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {T})")).scalar():
        raise RuntimeError(f"cannot downgrade 0011: records would be discarded from {T}")
    op.execute(f"DROP TRIGGER trg_{T}_guard ON {T}")
    op.execute("DROP FUNCTION protective_exit_requirements_guard()")
    op.drop_index("uq_protective_exit_requirements_open_per_position", table_name=T)
    op.drop_index(op.f(f"ix_{T}_user_id"), table_name=T)
    op.drop_index(op.f(f"ix_{T}_position_id"), table_name=T)
    op.drop_table(T)
