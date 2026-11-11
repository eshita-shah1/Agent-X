"""foundation

Revision ID: 0001
Revises: 
Create Date: 2026-11-11 11:32:15.257048
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('data_providers',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_providers')),
    sa.UniqueConstraint('code', name=op.f('uq_data_providers_code'))
    )
    op.create_table('instruments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('exchange', sa.String(length=16), nullable=False),
    sa.Column('instrument_type', sa.Enum('EQUITY', 'FUTURE', name='instrument_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=True),
    sa.Column('tick_size', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('lot_size', sa.Integer(), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instruments')),
    sa.UniqueConstraint('exchange', 'symbol', 'instrument_type', name='uq_instruments_identity')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('client_ip', postgresql.INET(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_events_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index(op.f('ix_audit_events_event_type'), 'audit_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_audit_events_user_id'), 'audit_events', ['user_id'], unique=False)
    op.create_table('candles',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('timeframe', sa.Enum('1m', '5m', '15m', '60m', '1d', name='timeframe', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('bar_start_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('bar_end_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('open', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('high', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('low', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('close', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('volume', sa.BigInteger(), nullable=False),
    sa.Column('is_final', sa.Boolean(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('price_basis', sa.Enum('UNVERIFIED_PROVIDER_NATIVE', 'SPLIT_ADJUSTED_VERIFIED', 'AS_TRADED_VERIFIED', name='price_basis', native_enum=False, create_constraint=True, length=32), server_default='UNVERIFIED_PROVIDER_NATIVE', nullable=False),
    sa.Column('basis_verified_by', sa.String(length=128), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('bar_end_ts > bar_start_ts', name=op.f('ck_candles_bar_interval_valid')),
    sa.CheckConstraint('high >= low AND high >= open AND high >= close AND low <= open AND low <= close AND low > 0', name=op.f('ck_candles_ohlc_valid')),
    sa.CheckConstraint('volume >= 0', name=op.f('ck_candles_volume_non_negative')),
    sa.CheckConstraint("price_basis = 'UNVERIFIED_PROVIDER_NATIVE' OR basis_verified_by IS NOT NULL", name=op.f('ck_candles_verified_basis_has_evidence')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_candles_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_candles_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_candles')),
    sa.UniqueConstraint('instrument_id', 'provider_id', 'timeframe', 'bar_start_ts', name='uq_candles_identity')
    )
    op.create_index('ix_candles_instrument_timeframe_start', 'candles', ['instrument_id', 'timeframe', 'bar_start_ts'], unique=False)
    op.create_table('corporate_actions',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('action_type', sa.Enum('SPLIT', 'BONUS', 'DIVIDEND', 'OTHER', name='corporate_action_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('ex_date', sa.Date(), nullable=False),
    sa.Column('ratio_numerator', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('ratio_denominator', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('cash_amount', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('source', sa.String(length=128), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(action_type <> 'DIVIDEND') OR (cash_amount IS NOT NULL AND cash_amount >= 0)", name=op.f('ck_corporate_actions_cash_required_for_dividend')),
    sa.CheckConstraint("(action_type NOT IN ('SPLIT','BONUS')) OR (ratio_numerator IS NOT NULL AND ratio_denominator IS NOT NULL AND ratio_numerator > 0 AND ratio_denominator > 0)", name=op.f('ck_corporate_actions_ratio_required_for_split_bonus')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_corporate_actions_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_corporate_actions')),
    sa.UniqueConstraint('instrument_id', 'action_type', 'ex_date', 'source', name='uq_corporate_actions_identity')
    )
    op.create_index(op.f('ix_corporate_actions_instrument_id'), 'corporate_actions', ['instrument_id'], unique=False)
    op.create_table('virtual_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('initial_capital', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('initial_capital > 0', name=op.f('ck_virtual_accounts_initial_capital_positive')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_virtual_accounts_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_virtual_accounts')),
    sa.UniqueConstraint('user_id', name='uq_virtual_accounts_user_id')
    )
    op.create_index(op.f('ix_virtual_accounts_user_id'), 'virtual_accounts', ['user_id'], unique=False)
    op.create_table('adjusted_candles',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('candle_id', sa.BigInteger(), nullable=False),
    sa.Column('adjustment_method', sa.Enum('SPLIT', 'SPLIT_AND_DIVIDEND', 'PROVIDER_ADJUSTED', name='adjustment_method', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('adjustment_source', sa.String(length=128), nullable=False),
    sa.Column('corporate_action_set_version', sa.String(length=64), nullable=False),
    sa.Column('adjustment_factor', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('adj_open', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('adj_high', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('adj_low', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('adj_close', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('adj_volume', sa.BigInteger(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('adj_high >= adj_low AND adj_high >= adj_open AND adj_high >= adj_close AND adj_low <= adj_open AND adj_low <= adj_close AND adj_low > 0', name=op.f('ck_adjusted_candles_adj_ohlc_valid')),
    sa.CheckConstraint('adj_volume IS NULL OR adj_volume >= 0', name=op.f('ck_adjusted_candles_adj_volume_non_negative')),
    sa.CheckConstraint('adjustment_factor > 0', name=op.f('ck_adjusted_candles_factor_positive')),
    sa.ForeignKeyConstraint(['candle_id'], ['candles.id'], name=op.f('fk_adjusted_candles_candle_id_candles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_adjusted_candles')),
    sa.UniqueConstraint('candle_id', 'adjustment_method', 'corporate_action_set_version', name='uq_adjusted_candles_identity')
    )
    op.create_index(op.f('ix_adjusted_candles_candle_id'), 'adjusted_candles', ['candle_id'], unique=False)

    # --- Hand-written: raw price immutability -------------------------------------------
    # Provider-native candles can never be overwritten, so adjusted data cannot replace them.
    # Only the verification label (price_basis, basis_verified_by) may change after insert.
    op.execute("""
        CREATE FUNCTION candles_reject_immutable_update() RETURNS trigger AS $$
        BEGIN
            IF (NEW.id, NEW.instrument_id, NEW.provider_id, NEW.timeframe, NEW.bar_start_ts,
                NEW.bar_end_ts, NEW.session_date, NEW.open, NEW.high, NEW.low, NEW.close,
                NEW.volume, NEW.is_final, NEW.observed_at, NEW.ingested_at)
               IS DISTINCT FROM
               (OLD.id, OLD.instrument_id, OLD.provider_id, OLD.timeframe, OLD.bar_start_ts,
                OLD.bar_end_ts, OLD.session_date, OLD.open, OLD.high, OLD.low, OLD.close,
                OLD.volume, OLD.is_final, OLD.observed_at, OLD.ingested_at)
            THEN
                RAISE EXCEPTION 'candles rows are immutable: only price_basis and basis_verified_by may be updated'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_candles_immutable BEFORE UPDATE ON candles
        FOR EACH ROW EXECUTE FUNCTION candles_reject_immutable_update();
    """)

    # --- Hand-written: audit events are append-only -------------------------------------
    op.execute("""
        CREATE FUNCTION audit_events_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_reject_change();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION audit_events_reject_change()")
    op.execute("DROP TRIGGER trg_candles_immutable ON candles")
    op.execute("DROP FUNCTION candles_reject_immutable_update()")
    op.drop_index(op.f('ix_adjusted_candles_candle_id'), table_name='adjusted_candles')
    op.drop_table('adjusted_candles')
    op.drop_index(op.f('ix_virtual_accounts_user_id'), table_name='virtual_accounts')
    op.drop_table('virtual_accounts')
    op.drop_index(op.f('ix_corporate_actions_instrument_id'), table_name='corporate_actions')
    op.drop_table('corporate_actions')
    op.drop_index('ix_candles_instrument_timeframe_start', table_name='candles')
    op.drop_table('candles')
    op.drop_index(op.f('ix_audit_events_user_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_event_type'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_table('users')
    op.drop_table('instruments')
    op.drop_table('data_providers')
