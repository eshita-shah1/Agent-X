"""next-bar execution model, component fees, risk refresh (Phase 4 follow-up)

Additive:
  * orders.accepted_at (decision time on the trading clock; only bars starting at or after it can fill the
    order). Existing rows are backfilled from created_at.
  * orders.risk_budget (approved risk amount for an entry; caps fills when the next open gaps).
  * fills.fee_breakdown (per-component Indian-equity fees).
  * risk_state.last_equity / last_equity_at (visibility of how current the risk references are).
The order guard trigger is replaced so accepted_at and risk_budget are immutable like the other identity
columns. Downgrade is refused while any order or fill exists (same convention as 0003/0004).

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_BASE_IDENTITY = (
    "id", "user_id", "account_id", "instrument_id", "client_order_id", "request_fingerprint", "source",
    "strategy_id", "side", "order_type", "requested_quantity", "quantity", "limit_price", "stop_loss_price",
    "take_profit_price", "created_at",
)


def _guard_function(identity: tuple[str, ...]) -> str:
    new = ", ".join(f"NEW.{c}" for c in identity)
    old = ", ".join(f"OLD.{c}" for c in identity)
    return f"""
        CREATE OR REPLACE FUNCTION orders_guard_change() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'orders cannot be deleted'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status IN ('FILLED', 'REJECTED', 'CANCELLED') THEN
                RAISE EXCEPTION 'order in terminal state % cannot change', OLD.status
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF ({new}) IS DISTINCT FROM ({old}) THEN
                RAISE EXCEPTION 'order identity columns are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.filled_quantity < OLD.filled_quantity THEN
                RAISE EXCEPTION 'filled_quantity cannot decrease'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


def upgrade() -> None:
    op.add_column("orders", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    # Backfill existing orders. The guard trigger forbids updates of terminal orders, so it is disabled for
    # this one statement only (inside the migration transaction).
    op.execute("ALTER TABLE orders DISABLE TRIGGER trg_orders_guard")
    op.execute("UPDATE orders SET accepted_at = created_at WHERE accepted_at IS NULL")
    op.execute("ALTER TABLE orders ENABLE TRIGGER trg_orders_guard")
    op.alter_column("orders", "accepted_at", nullable=False)
    op.add_column("orders", sa.Column("risk_budget", sa.Numeric(precision=24, scale=6), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_risk_budget_non_negative"), "orders", "risk_budget IS NULL OR risk_budget >= 0"
    )
    op.add_column(
        "fills",
        sa.Column("fee_breakdown", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("risk_state", sa.Column("last_equity", sa.Numeric(precision=24, scale=6), nullable=True))
    op.add_column("risk_state", sa.Column("last_equity_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(_guard_function(_BASE_IDENTITY + ("accepted_at", "risk_budget")))


def downgrade() -> None:
    conn = op.get_bind()
    holding = [t for t in ("orders", "fills") if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()]
    if holding:
        raise RuntimeError(f"cannot downgrade 0005: execution data would be discarded from {', '.join(holding)}")
    op.execute(_guard_function(_BASE_IDENTITY))
    op.drop_column("risk_state", "last_equity_at")
    op.drop_column("risk_state", "last_equity")
    op.drop_column("fills", "fee_breakdown")
    op.drop_constraint(op.f("ck_orders_risk_budget_non_negative"), "orders", type_="check")
    op.drop_column("orders", "risk_budget")
    op.drop_column("orders", "accepted_at")
