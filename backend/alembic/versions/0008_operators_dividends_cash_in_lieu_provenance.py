"""operator role, dividend credits (B5), cash-in-lieu provenance

Additive; no existing row is rewritten:
  * users.is_operator (default false; set only by the server-side CLI).
  * dividend_credits: append-only record of operator-confirmed gross dividend credits (partial unique index: one
    APPLIED row per user and dividend).
  * cash_ledger: entries may belong to a dividend credit (DIVIDEND, > 0). The source and sign checks are replaced;
    the replacements are added NOT VALID, so they bind every new row without re-validating historical ones.
  * corporate_action_adjustments: cash_in_lieu_source / cash_in_lieu_observed_at; a cash-in-lieu must carry both
    (NOT VALID check: binds new rows only).
Downgrade is refused while any order, fill, adjustment or dividend credit exists.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    op.add_column("users", sa.Column("is_operator", sa.Boolean(), server_default="false", nullable=False))

    op.create_table(
        "dividend_credits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("corporate_action_id", sa.BigInteger(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("cum_date", sa.Date(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("entitled_quantity", sa.BigInteger(), nullable=False),
        sa.Column("provider_amount_per_share", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("confirmed_amount_per_share", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("evidence_reference", sa.String(length=500), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", _enum("APPLIED", "REJECTED", name="dividend_credit_status"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("operator_id", sa.UUID(), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("entitled_quantity >= 0", name=op.f("ck_dividend_credits_entitled_quantity_non_negative")),
        sa.CheckConstraint("status <> 'APPLIED' OR entitled_quantity > 0", name=op.f("ck_dividend_credits_applied_has_entitlement")),
        sa.CheckConstraint("confirmed_amount_per_share > 0", name=op.f("ck_dividend_credits_confirmed_amount_positive")),
        sa.CheckConstraint("status <> 'APPLIED' OR gross_amount > 0", name=op.f("ck_dividend_credits_applied_amount_positive")),
        sa.CheckConstraint("status = 'APPLIED' OR reason IS NOT NULL", name=op.f("ck_dividend_credits_refusal_has_reason")),
        sa.CheckConstraint("cum_date < ex_date", name=op.f("ck_dividend_credits_cum_date_before_ex_date")),
        sa.ForeignKeyConstraint(["account_id"], ["virtual_accounts.id"], name=op.f("fk_dividend_credits_account_id_virtual_accounts"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["corporate_action_id"], ["corporate_actions.id"], name=op.f("fk_dividend_credits_corporate_action_id_corporate_actions"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name=op.f("fk_dividend_credits_instrument_id_instruments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], name=op.f("fk_dividend_credits_operator_id_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_dividend_credits_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dividend_credits")),
    )
    op.create_index(op.f("ix_dividend_credits_corporate_action_id"), "dividend_credits", ["corporate_action_id"])
    op.create_index(op.f("ix_dividend_credits_user_id"), "dividend_credits", ["user_id"])
    op.create_index(
        "uq_dividend_credits_applied_once", "dividend_credits", ["user_id", "corporate_action_id"],
        unique=True, postgresql_where=sa.text("status = 'APPLIED'"),
    )
    op.execute("""
        CREATE TRIGGER trg_dividend_credits_append_only BEFORE UPDATE OR DELETE ON dividend_credits
        FOR EACH ROW EXECUTE FUNCTION trading_reject_append_only_change();
    """)

    op.add_column("cash_ledger", sa.Column("dividend_credit_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_cash_ledger_dividend_credit_id_dividend_credits"), "cash_ledger", "dividend_credits",
        ["dividend_credit_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_cash_ledger_dividend_credit_id"), "cash_ledger", ["dividend_credit_id"])
    op.create_unique_constraint(
        "uq_cash_ledger_dividend_credit_entry_type", "cash_ledger", ["dividend_credit_id", "entry_type"]
    )
    op.drop_constraint(op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger",
        "entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE', 'CASH_IN_LIEU', 'DIVIDEND')",
    )
    op.drop_constraint(op.f("ck_cash_ledger_amount_sign_matches_type_v2"), "cash_ledger", type_="check")
    op.drop_constraint(op.f("ck_cash_ledger_exactly_one_source"), "cash_ledger", type_="check")
    op.execute(
        "ALTER TABLE cash_ledger ADD CONSTRAINT ck_cash_ledger_amount_sign_matches_type_v3 CHECK ("
        "(entry_type = 'BUY_SETTLEMENT' AND amount < 0) OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
        " OR (entry_type = 'FEE' AND amount <= 0) OR (entry_type = 'CASH_IN_LIEU' AND amount > 0)"
        " OR (entry_type = 'DIVIDEND' AND amount > 0)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE cash_ledger ADD CONSTRAINT ck_cash_ledger_exactly_one_source_v2 CHECK ("
        "(fill_id IS NOT NULL AND adjustment_id IS NULL AND dividend_credit_id IS NULL"
        " AND entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE'))"
        " OR (fill_id IS NULL AND adjustment_id IS NOT NULL AND dividend_credit_id IS NULL AND entry_type = 'CASH_IN_LIEU')"
        " OR (fill_id IS NULL AND adjustment_id IS NULL AND dividend_credit_id IS NOT NULL AND entry_type = 'DIVIDEND')"
        ") NOT VALID"
    )

    op.add_column("corporate_action_adjustments", sa.Column("cash_in_lieu_source", sa.String(length=500), nullable=True))
    op.add_column("corporate_action_adjustments", sa.Column("cash_in_lieu_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "ALTER TABLE corporate_action_adjustments ADD CONSTRAINT "
        "ck_corporate_action_adjustments_cash_in_lieu_has_provenance CHECK ("
        "cash_in_lieu IS NULL OR (cash_in_lieu_source IS NOT NULL AND cash_in_lieu_observed_at IS NOT NULL)) NOT VALID"
    )


def downgrade() -> None:
    conn = op.get_bind()
    holding = [
        t for t in ("orders", "fills", "corporate_action_adjustments", "dividend_credits")
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()
    ]
    if holding:
        raise RuntimeError(f"cannot downgrade 0008: records would be discarded from {', '.join(holding)}")
    op.drop_constraint(op.f("ck_corporate_action_adjustments_cash_in_lieu_has_provenance"), "corporate_action_adjustments", type_="check")
    op.drop_column("corporate_action_adjustments", "cash_in_lieu_observed_at")
    op.drop_column("corporate_action_adjustments", "cash_in_lieu_source")
    op.drop_constraint(op.f("ck_cash_ledger_exactly_one_source_v2"), "cash_ledger", type_="check")
    op.drop_constraint(op.f("ck_cash_ledger_amount_sign_matches_type_v3"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_exactly_one_source"), "cash_ledger",
        "(fill_id IS NOT NULL AND adjustment_id IS NULL AND entry_type <> 'CASH_IN_LIEU')"
        " OR (fill_id IS NULL AND adjustment_id IS NOT NULL AND entry_type = 'CASH_IN_LIEU')",
    )
    op.create_check_constraint(
        op.f("ck_cash_ledger_amount_sign_matches_type_v2"), "cash_ledger",
        "(entry_type = 'BUY_SETTLEMENT' AND amount < 0) OR (entry_type = 'SELL_PROCEEDS' AND amount > 0)"
        " OR (entry_type = 'FEE' AND amount <= 0) OR (entry_type = 'CASH_IN_LIEU' AND amount > 0)",
    )
    op.drop_constraint(op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_cash_ledger_ledger_entry_type"), "cash_ledger",
        "entry_type IN ('BUY_SETTLEMENT', 'SELL_PROCEEDS', 'FEE', 'CASH_IN_LIEU')",
    )
    op.drop_constraint("uq_cash_ledger_dividend_credit_entry_type", "cash_ledger", type_="unique")
    op.drop_index(op.f("ix_cash_ledger_dividend_credit_id"), table_name="cash_ledger")
    op.drop_constraint(op.f("fk_cash_ledger_dividend_credit_id_dividend_credits"), "cash_ledger", type_="foreignkey")
    op.drop_column("cash_ledger", "dividend_credit_id")
    op.execute("DROP TRIGGER trg_dividend_credits_append_only ON dividend_credits")
    op.drop_index("uq_dividend_credits_applied_once", table_name="dividend_credits")
    op.drop_index(op.f("ix_dividend_credits_user_id"), table_name="dividend_credits")
    op.drop_index(op.f("ix_dividend_credits_corporate_action_id"), table_name="dividend_credits")
    op.drop_table("dividend_credits")
    op.drop_column("users", "is_operator")
