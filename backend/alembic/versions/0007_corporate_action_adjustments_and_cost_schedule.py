"""corporate-action adjustments (B4), fill-level realized P&L (B10), order product (B3)

Additive; no existing fill or ledger row is modified:
  * corporate_action_adjustments: append-only audit of applying (or refusing to apply) a split/bonus to a position;
    a partial unique index allows at most one APPLIED row per position and corporate action.
  * cash_ledger: entries may now belong to an adjustment (CASH_IN_LIEU) instead of a fill; fill_id becomes nullable
    with a check that exactly one source is set; the entry-type and sign checks gain CASH_IN_LIEU (> 0).
  * fills.realized_pnl: realized P&L of each fill, attributed to its simulated fill time (NULL for older fills).
  * orders.product: DELIVERY / INTRADAY (existing rows get the column default DELIVERY, the only product executed
    so far); protected by the order guard trigger like the other identity columns.
Downgrade is refused while any order, fill or adjustment exists (same convention as 0003-0006).

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_IDENTITY_0006 = (
    "id", "user_id", "account_id", "instrument_id", "client_order_id", "request_fingerprint", "source",
    "strategy_id", "side", "order_type", "requested_quantity", "quantity", "limit_price", "stop_loss_price",
    "take_profit_price", "created_at", "accepted_at", "risk_budget", "signal_at", "decision_price_ref",
    "execution_policy",
)
_IDENTITY_0007 = _IDENTITY_0006 + ("product",)


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


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    op.create_table(
        "corporate_action_adjustments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("corporate_action_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", _enum("SPLIT", "BONUS", "DIVIDEND", "OTHER", name="corporate_action_type"), nullable=False),
        sa.Column("ratio_numerator", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("ratio_denominator", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("evidence_reference", sa.String(length=500), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", _enum("APPLIED", "REJECTED", "BLOCKED_FRACTIONAL", name="adjustment_status"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("original_quantity", sa.BigInteger(), nullable=False),
        sa.Column("original_avg_price", sa.Numeric(precision=24, scale=10), nullable=False),
        sa.Column("original_cost_basis", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("adjusted_quantity", sa.BigInteger(), nullable=True),
        sa.Column("adjusted_avg_price", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("adjusted_cost_basis", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("fractional_quantity", sa.Numeric(precision=24, scale=10), nullable=False),
        sa.Column("cash_in_lieu_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("cash_in_lieu", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=24, scale=6), server_default="0", nullable=False),
        sa.Column("original_stop_loss", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("adjusted_stop_loss", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("original_take_profit", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("adjusted_take_profit", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("cancelled_order_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("adjusted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("ratio_numerator > 0 AND ratio_denominator > 0", name=op.f("ck_corporate_action_adjustments_ratio_positive")),
        sa.CheckConstraint("original_quantity > 0", name=op.f("ck_corporate_action_adjustments_original_quantity_positive")),
        sa.CheckConstraint(
            "status <> 'APPLIED' OR (adjusted_quantity IS NOT NULL AND adjusted_avg_price IS NOT NULL "
            "AND adjusted_cost_basis IS NOT NULL AND adjusted_quantity >= 0)",
            name=op.f("ck_corporate_action_adjustments_applied_is_complete"),
        ),
        sa.CheckConstraint("status = 'APPLIED' OR reason IS NOT NULL", name=op.f("ck_corporate_action_adjustments_refusal_has_reason")),
        sa.CheckConstraint(
            "fractional_quantity >= 0 AND fractional_quantity < 1", name=op.f("ck_corporate_action_adjustments_fraction_below_one_share")
        ),
        sa.CheckConstraint(
            "(cash_in_lieu IS NULL) = (cash_in_lieu_price IS NULL)",
            name=op.f("ck_corporate_action_adjustments_cash_in_lieu_amount_and_price_together"),
        ),
        sa.ForeignKeyConstraint(["account_id"], ["virtual_accounts.id"], name=op.f("fk_corporate_action_adjustments_account_id_virtual_accounts"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["corporate_action_id"], ["corporate_actions.id"], name=op.f("fk_corporate_action_adjustments_corporate_action_id_corporate_actions"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name=op.f("fk_corporate_action_adjustments_instrument_id_instruments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], name=op.f("fk_corporate_action_adjustments_position_id_positions"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_corporate_action_adjustments_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_action_adjustments")),
    )
    op.create_index(op.f("ix_corporate_action_adjustments_corporate_action_id"), "corporate_action_adjustments", ["corporate_action_id"])
    op.create_index(op.f("ix_corporate_action_adjustments_position_id"), "corporate_action_adjustments", ["position_id"])
    op.create_index(op.f("ix_corporate_action_adjustments_user_id"), "corporate_action_adjustments", ["user_id"])
    op.create_index(
        "uq_corporate_action_adjustments_applied_once", "corporate_action_adjustments",
        ["position_id", "corporate_action_id"], unique=True, postgresql_where=sa.text("status = 'APPLIED'"),
    )
    op.execute("""
        CREATE TRIGGER trg_corporate_action_adjustments_append_only BEFORE UPDATE OR DELETE ON corporate_action_adjustments
        FOR EACH ROW EXECUTE FUNCTION trading_reject_append_only_change();
    """)

    # cash_ledger: entries from adjustments (cash-in-lieu). Schema only; existing rows are not touched.
    op.add_column("cash_ledger", sa.Column("adjustment_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_cash_ledger_adjustment_id_corporate_action_adjustments"), "cash_ledger",
        "corporate_action_adjustments", ["adjustment_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_cash_ledger_adjustment_id"), "cash_ledger", ["adjustment_id"])
    op.create_unique_constraint("uq_cash_ledger_adjustment_entry_type", "cash_ledger", ["adjustment_id", "entry_type"])
    op.alter_column("cash_ledger", "fill_id", nullable=True)
    op.drop_constraint(op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger",
        "entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE', 'CASH_IN_LIEU')",
    )
    op.drop_constraint(op.f("ck_cash_ledger_amount_sign_matches_type"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_amount_sign_matches_type_v2"), "cash_ledger",
        "(entry_type = 'BUY_SETTLEMENT' AND amount < 0) OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
        " OR (entry_type = 'FEE' AND amount <= 0) OR (entry_type = 'CASH_IN_LIEU' AND amount > 0)",
    )
    op.create_check_constraint(
        op.f("ck_cash_ledger_exactly_one_source"), "cash_ledger",
        "(fill_id IS NOT NULL AND adjustment_id IS NULL AND entry_type <> 'CASH_IN_LIEU')"
        " OR (fill_id IS NULL AND adjustment_id IS NOT NULL AND entry_type = 'CASH_IN_LIEU')",
    )

    op.add_column("fills", sa.Column("realized_pnl", sa.Numeric(precision=24, scale=6), nullable=True))
    op.add_column("orders", sa.Column("product", _enum("DELIVERY", "INTRADAY", name="product"), server_default="DELIVERY", nullable=False))
    op.execute(_guard_function(_IDENTITY_0007))


def downgrade() -> None:
    conn = op.get_bind()
    holding = [
        t for t in ("orders", "fills", "corporate_action_adjustments")
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()
    ]
    if holding:
        raise RuntimeError(f"cannot downgrade 0007: execution records would be discarded from {', '.join(holding)}")
    op.execute(_guard_function(_IDENTITY_0006))
    op.drop_column("orders", "product")
    op.drop_column("fills", "realized_pnl")
    op.drop_constraint(op.f("ck_cash_ledger_exactly_one_source"), "cash_ledger", type_="check")
    op.drop_constraint(op.f("ck_cash_ledger_amount_sign_matches_type_v2"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_amount_sign_matches_type"), "cash_ledger",
        "(entry_type = 'BUY_SETTLEMENT' AND amount < 0) OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
        " OR (entry_type = 'FEE' AND amount <= 0)",
    )
    op.drop_constraint(op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger", "entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE')"
    )
    op.alter_column("cash_ledger", "fill_id", nullable=False)
    op.drop_constraint("uq_cash_ledger_adjustment_entry_type", "cash_ledger", type_="unique")
    op.drop_index(op.f("ix_cash_ledger_adjustment_id"), table_name="cash_ledger")
    op.drop_constraint(op.f("fk_cash_ledger_adjustment_id_corporate_action_adjustments"), "cash_ledger", type_="foreignkey")
    op.drop_column("cash_ledger", "adjustment_id")
    op.execute("DROP TRIGGER trg_corporate_action_adjustments_append_only ON corporate_action_adjustments")
    op.drop_index("uq_corporate_action_adjustments_applied_once", table_name="corporate_action_adjustments")
    op.drop_index(op.f("ix_corporate_action_adjustments_user_id"), table_name="corporate_action_adjustments")
    op.drop_index(op.f("ix_corporate_action_adjustments_position_id"), table_name="corporate_action_adjustments")
    op.drop_index(op.f("ix_corporate_action_adjustments_corporate_action_id"), table_name="corporate_action_adjustments")
    op.drop_table("corporate_action_adjustments")
