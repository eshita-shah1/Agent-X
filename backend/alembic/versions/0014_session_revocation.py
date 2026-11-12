"""session revocation and login-lockout index (Phase 8)

Additive: one nullable column. Access tokens issued at or before this instant are refused, so POST /auth/logout (and an
operator deactivating an account) ends every existing session without a token blacklist. No existing row changes.
A partial expression index serves the login-lockout count (failed logins per email within a window): measured on 100k
failed-login rows, 83.9 ms (sequential scan, every login) -> 0.108 ms. Downgrade drops both (neither holds trading or
accounting data); revoked tokens would then be accepted again until they expire.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


INDEX = "ix_audit_events_login_failed_email"


def upgrade() -> None:
    op.add_column("users", sa.Column("sessions_revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(INDEX, "audit_events", [sa.text("(details ->> 'email')"), "created_at"],
                    postgresql_where=sa.text("event_type = 'auth.login_failed'"))


def downgrade() -> None:
    op.drop_index(INDEX, table_name="audit_events")
    op.drop_column("users", "sessions_revoked_at")
