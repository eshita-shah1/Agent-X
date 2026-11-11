"""execution approval reference on orders and fills (D4/B1 auditability)

Additive; no existing row is rewritten:
  * orders.execution_approval_reference: the EXECUTION_APPROVAL_REFERENCE in force when the order was accepted
    (NULL while execution was disabled). Added to the order guard trigger's immutable identity columns. A NOT VALID
    check requires it whenever an execution policy is recorded, binding new rows without re-validating history.
  * fills.execution_approval_reference: copied from the order. NOT VALID check: required on new fills. A BEFORE INSERT
    trigger requires it to equal the order's value; fills are already append-only (trg_fills_append_only).
Downgrade is refused while any order or fill carries a reference (the audit trail would be discarded).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_IDENTITY_0007 = (
    "id", "user_id", "account_id", "instrument_id", "client_order_id", "request_fingerprint", "source",
    "strategy_id", "side", "order_type", "requested_quantity", "quantity", "limit_price", "stop_loss_price",
    "take_profit_price", "created_at", "accepted_at", "risk_budget", "signal_at", "decision_price_ref",
    "execution_policy", "product",
)
_IDENTITY_0010 = _IDENTITY_0007 + ("execution_approval_reference",)


def _guard_function(identity: tuple[str, ...]) -> str:
    """Same body as 0007's guard; only the identity column list changes."""
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
    op.add_column("orders", sa.Column("execution_approval_reference", sa.String(length=128), nullable=True))
    op.add_column("fills", sa.Column("execution_approval_reference", sa.String(length=128), nullable=True))
    op.execute(
        "ALTER TABLE orders ADD CONSTRAINT ck_orders_policy_has_approval_reference CHECK ("
        "execution_policy IS NULL OR execution_approval_reference IS NOT NULL) NOT VALID"
    )
    op.execute(
        "ALTER TABLE fills ADD CONSTRAINT ck_fills_has_approval_reference CHECK ("
        "execution_approval_reference IS NOT NULL) NOT VALID"
    )
    op.execute(_guard_function(_IDENTITY_0010))
    op.execute(
        """
        CREATE FUNCTION fills_approval_matches_order() RETURNS trigger AS $$
        BEGIN
            IF NEW.execution_approval_reference IS DISTINCT FROM
               (SELECT o.execution_approval_reference FROM orders o WHERE o.id = NEW.order_id) THEN
                RAISE EXCEPTION 'a fill must carry its order''s execution approval reference'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_fills_approval_matches_order BEFORE INSERT ON fills "
        "FOR EACH ROW EXECUTE FUNCTION fills_approval_matches_order()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    holding = [
        t for t in ("orders", "fills")
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t} WHERE execution_approval_reference IS NOT NULL)")).scalar()
    ]
    if holding:
        raise RuntimeError(f"cannot downgrade 0010: approval references would be discarded from {', '.join(holding)}")
    op.execute("DROP TRIGGER trg_fills_approval_matches_order ON fills")
    op.execute("DROP FUNCTION fills_approval_matches_order()")
    op.execute(_guard_function(_IDENTITY_0007))
    op.drop_constraint(op.f("ck_fills_has_approval_reference"), "fills", type_="check")
    op.drop_constraint(op.f("ck_orders_policy_has_approval_reference"), "orders", type_="check")
    op.drop_column("fills", "execution_approval_reference")
    op.drop_column("orders", "execution_approval_reference")
