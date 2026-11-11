"""simulation-only execution policy, signal time and decision reference (Phase 4, D4 simulation decision)

Additive:
  * orders.signal_at (NOT NULL, <= accepted_at; existing rows backfilled from accepted_at),
    orders.decision_price_ref (the reference bar the decision used), orders.execution_policy (policy in force at
    acceptance; existing orders that have fills are VERIFIED_NEXT_BAR_OPEN, the only policy before 0006).
  * fills.execution_policy (NOT NULL; existing rows VERIFIED_NEXT_BAR_OPEN).
  * ck_fills_price_basis_verified (0004) is replaced by ck_fills_price_basis_matches_policy: verified fills never carry
    UNVERIFIED_PROVIDER_NATIVE, and simulation fills always do, so a simulated fill can never pose as a verified one.
  * The order guard trigger also protects signal_at, decision_price_ref and execution_policy.
Backfills run with the guard / append-only triggers disabled for those statements only, inside the migration
transaction. Downgrade is refused while any order or fill exists (same convention as 0003-0005).

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_POLICY = sa.Enum(
    "VERIFIED_NEXT_BAR_OPEN", "SIMULATION_PROVIDER_NATIVE", name="execution_policy", native_enum=False,
    create_constraint=True, length=32,
)
_IDENTITY_0005 = (
    "id", "user_id", "account_id", "instrument_id", "client_order_id", "request_fingerprint", "source",
    "strategy_id", "side", "order_type", "requested_quantity", "quantity", "limit_price", "stop_loss_price",
    "take_profit_price", "created_at", "accepted_at", "risk_budget",
)
_IDENTITY_0006 = _IDENTITY_0005 + ("signal_at", "decision_price_ref", "execution_policy")


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
    op.add_column("orders", sa.Column("signal_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("decision_price_ref", sa.String(length=64), nullable=True))
    op.add_column("orders", sa.Column("execution_policy", _POLICY, nullable=True))
    op.add_column("fills", sa.Column("execution_policy", _POLICY, nullable=True))

    op.execute("ALTER TABLE orders DISABLE TRIGGER trg_orders_guard")
    op.execute("UPDATE orders SET signal_at = accepted_at WHERE signal_at IS NULL")
    op.execute(
        "UPDATE orders SET execution_policy = 'VERIFIED_NEXT_BAR_OPEN' "
        "WHERE execution_policy IS NULL AND EXISTS (SELECT 1 FROM fills f WHERE f.order_id = orders.id)"
    )
    op.execute("ALTER TABLE orders ENABLE TRIGGER trg_orders_guard")
    op.execute("ALTER TABLE fills DISABLE TRIGGER trg_fills_append_only")
    op.execute("UPDATE fills SET execution_policy = 'VERIFIED_NEXT_BAR_OPEN' WHERE execution_policy IS NULL")
    op.execute("ALTER TABLE fills ENABLE TRIGGER trg_fills_append_only")

    op.alter_column("orders", "signal_at", nullable=False)
    op.alter_column("fills", "execution_policy", nullable=False)
    op.create_check_constraint(op.f("ck_orders_signal_not_after_acceptance"), "orders", "signal_at <= accepted_at")
    op.drop_constraint(op.f("ck_fills_price_basis_verified"), "fills", type_="check")
    op.create_check_constraint(
        op.f("ck_fills_price_basis_matches_policy"),
        "fills",
        "(execution_policy = 'VERIFIED_NEXT_BAR_OPEN' AND price_basis <> 'UNVERIFIED_PROVIDER_NATIVE')"
        " OR (execution_policy = 'SIMULATION_PROVIDER_NATIVE' AND price_basis = 'UNVERIFIED_PROVIDER_NATIVE')",
    )
    op.execute(_guard_function(_IDENTITY_0006))


def downgrade() -> None:
    conn = op.get_bind()
    holding = [t for t in ("orders", "fills") if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()]
    if holding:
        raise RuntimeError(f"cannot downgrade 0006: execution records would be discarded from {', '.join(holding)}")
    op.execute(_guard_function(_IDENTITY_0005))
    op.drop_constraint(op.f("ck_fills_price_basis_matches_policy"), "fills", type_="check")
    op.create_check_constraint(
        op.f("ck_fills_price_basis_verified"), "fills", "price_basis <> 'UNVERIFIED_PROVIDER_NATIVE'"
    )
    op.drop_constraint(op.f("ck_orders_signal_not_after_acceptance"), "orders", type_="check")
    op.drop_column("fills", "execution_policy")
    op.drop_column("orders", "execution_policy")
    op.drop_column("orders", "decision_price_ref")
    op.drop_column("orders", "signal_at")
