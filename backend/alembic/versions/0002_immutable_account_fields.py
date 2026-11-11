"""make virtual account baseline fields immutable

initial_capital is the original account baseline used for historical performance
calculations. It, the account's currency and its owner must never change after creation.
A future account reset has to be a separate, explicitly authorised and audited operation
(not an UPDATE of these columns), and is out of scope for Phase 1.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION virtual_accounts_reject_immutable_update() RETURNS trigger AS $$
        BEGIN
            IF (NEW.id, NEW.user_id, NEW.currency, NEW.initial_capital, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.user_id, OLD.currency, OLD.initial_capital, OLD.created_at)
            THEN
                RAISE EXCEPTION 'virtual_accounts baseline fields are immutable (initial_capital, currency, user_id)'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_virtual_accounts_immutable BEFORE UPDATE ON virtual_accounts
        FOR EACH ROW EXECUTE FUNCTION virtual_accounts_reject_immutable_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_virtual_accounts_immutable ON virtual_accounts")
    op.execute("DROP FUNCTION virtual_accounts_reject_immutable_update()")
