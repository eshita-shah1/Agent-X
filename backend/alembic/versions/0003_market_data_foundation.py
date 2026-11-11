"""market data foundation

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11 13:13:52.595294
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 1 had no write path to candles/corporate_actions, so they must be empty. The NOT NULL provenance
    # columns added below cannot be back-filled honestly; refuse to guess if that ever turns out to be false.
    conn = op.get_bind()
    for table in ("candles", "corporate_actions", "adjusted_candles"):
        if conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError(f"cannot apply 0003: {table} already contains rows (provenance cannot be invented)")
    op.create_table('exchange_calendar_days',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('exchange', sa.String(length=16), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('kind', sa.Enum('HOLIDAY', 'SPECIAL_SESSION', name='calendar_day_kind', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('open_time', sa.Time(), nullable=True),
    sa.Column('close_time', sa.Time(), nullable=True),
    sa.Column('source', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind <> 'SPECIAL_SESSION' OR (open_time IS NOT NULL AND close_time IS NOT NULL AND close_time > open_time)", name=op.f('ck_exchange_calendar_days_special_session_has_times')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exchange_calendar_days')),
    sa.UniqueConstraint('exchange', 'day', name='uq_exchange_calendar_days_exchange_day')
    )
    op.create_table('data_ingestion_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('job_type', sa.Enum('BACKFILL', 'INCREMENTAL', name='job_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'INTERRUPTED', name='job_status', native_enum=False, create_constraint=True, length=32), server_default='QUEUED', nullable=False),
    sa.Column('requested_by_user_id', sa.UUID(), nullable=True),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('timeframe', sa.Enum('1m', '5m', '15m', '60m', '1d', name='timeframe', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('lock_key', sa.String(length=32), server_default='market-ingestion', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_data_ingestion_jobs_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], name=op.f('fk_data_ingestion_jobs_requested_by_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_ingestion_jobs'))
    )
    op.create_index(op.f('ix_data_ingestion_jobs_requested_by_user_id'), 'data_ingestion_jobs', ['requested_by_user_id'], unique=False)
    op.create_index('uq_ingestion_jobs_single_active', 'data_ingestion_jobs', ['lock_key'], unique=True, postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"))
    op.create_table('instrument_provider_symbols',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('provider_symbol', sa.String(length=64), nullable=False),
    sa.Column('verification_status', sa.Enum('UNVERIFIED', 'VERIFIED', 'FAILED', name='verification_status', native_enum=False, create_constraint=True, length=32), server_default='UNVERIFIED', nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verification_evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_instrument_provider_symbols_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_instrument_provider_symbols_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instrument_provider_symbols')),
    sa.UniqueConstraint('instrument_id', 'provider_id', name='uq_instrument_provider_symbols_instrument_provider'),
    sa.UniqueConstraint('provider_id', 'provider_symbol', name='uq_instrument_provider_symbols_provider_symbol')
    )
    op.create_table('provider_capabilities',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('capability', sa.Enum('HISTORICAL_DAILY_CANDLES', 'HISTORICAL_INTRADAY_CANDLES', 'FIVE_MINUTE_INTERVAL', 'LATEST_CANDLE', 'CORPORATE_ACTIONS', 'QUOTE_RETRIEVAL', 'INSTRUMENT_METADATA', 'ADJUSTED_INTRADAY_PRICES', 'FUTURES_CONTRACT_DATA', name='capability', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('status', sa.Enum('DOCUMENTED', 'VERIFIED', 'UNSUPPORTED', 'UNKNOWN', name='capability_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('probe_id', sa.String(length=64), nullable=True),
    sa.Column('determined_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_provider_capabilities_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_capabilities')),
    sa.UniqueConstraint('provider_id', 'capability', name='uq_provider_capabilities_provider_capability')
    )
    op.create_table('provider_health_state',
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error_kind', sa.String(length=32), nullable=True),
    sa.Column('last_error_message', sa.Text(), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_observed_market_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_successful_ingestion_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_provider_health_state_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('provider_id', name=op.f('pk_provider_health_state'))
    )
    op.create_table('data_ingestion_batches',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('timeframe', sa.Enum('1m', '5m', '15m', '60m', '1d', name='timeframe', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('provider_symbol', sa.String(length=64), nullable=False),
    sa.Column('request_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('request_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('RUNNING', 'SUCCEEDED', 'NO_DATA', 'FAILED', name='batch_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('rows_received', sa.Integer(), server_default='0', nullable=False),
    sa.Column('candles_inserted', sa.Integer(), server_default='0', nullable=False),
    sa.Column('duplicates_identical', sa.Integer(), server_default='0', nullable=False),
    sa.Column('restatements', sa.Integer(), server_default='0', nullable=False),
    sa.Column('quarantined', sa.Integer(), server_default='0', nullable=False),
    sa.Column('non_final_dropped', sa.Integer(), server_default='0', nullable=False),
    sa.Column('corporate_actions_recorded', sa.Integer(), server_default='0', nullable=False),
    sa.Column('response_first_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_last_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_kind', sa.String(length=32), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_data_ingestion_batches_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['job_id'], ['data_ingestion_jobs.id'], name=op.f('fk_data_ingestion_batches_job_id_data_ingestion_jobs'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_data_ingestion_batches_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_ingestion_batches'))
    )
    op.create_index('ix_data_ingestion_batches_instrument_started', 'data_ingestion_batches', ['instrument_id', 'started_at'], unique=False)
    op.create_index(op.f('ix_data_ingestion_batches_job_id'), 'data_ingestion_batches', ['job_id'], unique=False)
    op.create_table('data_quality_issues',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('batch_id', sa.BigInteger(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('issue_type', sa.Enum('NAIVE_TIMESTAMP', 'INVALID_TIMESTAMP', 'MISALIGNED_TIMESTAMP', 'OUT_OF_SESSION', 'NON_NUMERIC_VALUE', 'NON_FINITE_VALUE', 'NON_POSITIVE_PRICE', 'INVALID_OHLC', 'INVALID_VOLUME', 'DUPLICATE_IDENTICAL', 'DUPLICATE_CONFLICTING', 'INCOMPLETE_CANDLE', 'UNEXPECTED_INTERVAL', 'MISSING_INTERVALS', 'RESTATEMENT', 'CORPORATE_ACTIONS_UNAVAILABLE', 'UNRECOVERABLE_GAP', 'WINDOW_EDGE_TRUNCATED', name='issue_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('severity', sa.Enum('INFO', 'WARNING', 'ERROR', name='severity', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('bar_start_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['data_ingestion_batches.id'], name=op.f('fk_data_quality_issues_batch_id_data_ingestion_batches'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_data_quality_issues_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_quality_issues'))
    )
    op.create_index(op.f('ix_data_quality_issues_batch_id'), 'data_quality_issues', ['batch_id'], unique=False)
    op.create_index('ix_data_quality_issues_instrument_type', 'data_quality_issues', ['instrument_id', 'issue_type'], unique=False)
    op.create_table('data_restatement_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('timeframe', sa.Enum('1m', '5m', '15m', '60m', '1d', name='timeframe', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('bar_start_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('original_candle_id', sa.BigInteger(), nullable=False),
    sa.Column('restated_candle_id', sa.BigInteger(), nullable=False),
    sa.Column('batch_id', sa.BigInteger(), nullable=False),
    sa.Column('changed_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['data_ingestion_batches.id'], name=op.f('fk_data_restatement_events_batch_id_data_ingestion_batches'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], name=op.f('fk_data_restatement_events_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['original_candle_id'], ['candles.id'], name=op.f('fk_data_restatement_events_original_candle_id_candles'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['provider_id'], ['data_providers.id'], name=op.f('fk_data_restatement_events_provider_id_data_providers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['restated_candle_id'], ['candles.id'], name=op.f('fk_data_restatement_events_restated_candle_id_candles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_restatement_events')),
    sa.UniqueConstraint('restated_candle_id', name='uq_data_restatement_events_restated_candle_id')
    )
    op.create_index(op.f('ix_data_restatement_events_instrument_id'), 'data_restatement_events', ['instrument_id'], unique=False)
    op.add_column('candles', sa.Column('provider_symbol', sa.String(length=64), nullable=False))
    op.add_column('candles', sa.Column('ingestion_batch_id', sa.BigInteger(), nullable=False))
    op.add_column('candles', sa.Column('source_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))
    op.add_column('candles', sa.Column('observation_version', sa.Integer(), server_default='1', nullable=False))
    op.add_column('candles', sa.Column('is_canonical', sa.Boolean(), server_default='true', nullable=False))
    op.drop_constraint(op.f('uq_candles_identity'), 'candles', type_='unique')
    op.create_index(op.f('ix_candles_ingestion_batch_id'), 'candles', ['ingestion_batch_id'], unique=False)
    op.create_index('uq_candles_canonical', 'candles', ['instrument_id', 'provider_id', 'timeframe', 'bar_start_ts'], unique=True, postgresql_where=sa.text('is_canonical'))
    op.create_unique_constraint('uq_candles_observation', 'candles', ['instrument_id', 'provider_id', 'timeframe', 'bar_start_ts', 'observation_version'])
    op.create_foreign_key(op.f('fk_candles_ingestion_batch_id_data_ingestion_batches'), 'candles', 'data_ingestion_batches', ['ingestion_batch_id'], ['id'], ondelete='RESTRICT')
    op.add_column('corporate_actions', sa.Column('provider_id', sa.Integer(), nullable=False))
    op.add_column('corporate_actions', sa.Column('ingestion_batch_id', sa.BigInteger(), nullable=True))
    op.add_column('corporate_actions', sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False))
    op.add_column('corporate_actions', sa.Column('provider_raw', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))
    op.create_foreign_key(op.f('fk_corporate_actions_ingestion_batch_id_data_ingestion_batches'), 'corporate_actions', 'data_ingestion_batches', ['ingestion_batch_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_corporate_actions_provider_id_data_providers'), 'corporate_actions', 'data_providers', ['provider_id'], ['id'], ondelete='RESTRICT')
    op.add_column('instruments', sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False))
    op.add_column('instruments', sa.Column('contract_identifier', sa.String(length=64), nullable=True))
    op.add_column('instruments', sa.Column('expiry_date', sa.Date(), nullable=True))
    op.add_column('instruments', sa.Column('trading_status', sa.Enum('ENABLED', 'DISABLED', name='trading_status', native_enum=False, create_constraint=True, length=32), server_default='ENABLED', nullable=False))
    op.add_column('instruments', sa.Column('disabled_reason', sa.Text(), nullable=True))

    # --- Hand-written: CHECK constraints (Alembic does not autogenerate these on existing tables) ---
    # (the trading_status enum CHECK is created by add_column above)
    op.create_check_constraint(op.f('ck_instruments_futures_must_be_disabled'), 'instruments', "instrument_type <> 'FUTURE' OR trading_status = 'DISABLED'")
    op.create_check_constraint(op.f('ck_instruments_disabled_requires_reason'), 'instruments', "trading_status = 'ENABLED' OR disabled_reason IS NOT NULL")
    op.create_check_constraint(op.f('ck_instruments_equity_has_no_contract'), 'instruments', "instrument_type <> 'EQUITY' OR (expiry_date IS NULL AND contract_identifier IS NULL)")
    op.create_check_constraint(op.f('ck_candles_only_finalized_archived'), 'candles', "is_final")
    op.create_check_constraint(op.f('ck_candles_version_positive'), 'candles', "observation_version >= 1")

    # --- Hand-written: extend the candle immutability trigger to the new provenance/version columns ---
    # Only price_basis and basis_verified_by (the verification label) may change after insert.
    op.execute("""
        CREATE OR REPLACE FUNCTION candles_reject_immutable_update() RETURNS trigger AS $$
        BEGIN
            IF (NEW.id, NEW.instrument_id, NEW.provider_id, NEW.timeframe, NEW.bar_start_ts,
                NEW.bar_end_ts, NEW.session_date, NEW.open, NEW.high, NEW.low, NEW.close,
                NEW.volume, NEW.is_final, NEW.observed_at, NEW.ingested_at,
                NEW.provider_symbol, NEW.ingestion_batch_id, NEW.source_metadata,
                NEW.observation_version, NEW.is_canonical)
               IS DISTINCT FROM
               (OLD.id, OLD.instrument_id, OLD.provider_id, OLD.timeframe, OLD.bar_start_ts,
                OLD.bar_end_ts, OLD.session_date, OLD.open, OLD.high, OLD.low, OLD.close,
                OLD.volume, OLD.is_final, OLD.observed_at, OLD.ingested_at,
                OLD.provider_symbol, OLD.ingestion_batch_id, OLD.source_metadata,
                OLD.observation_version, OLD.is_canonical)
            THEN
                RAISE EXCEPTION 'candles rows are immutable: only price_basis and basis_verified_by may be updated'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


# Everything the downgrade would drop: rows in the tables 0003 creates, rows in Phase 1 tables that gain NOT NULL
# provenance columns here, and instruments carrying 0003 registry fields (dropping trading_status would silently
# make disabled futures look like ordinary instruments).
_DOWNGRADE_GUARDS = {
    "candles": "SELECT 1 FROM candles",
    "corporate_actions": "SELECT 1 FROM corporate_actions",
    "data_ingestion_jobs": "SELECT 1 FROM data_ingestion_jobs",
    "data_ingestion_batches": "SELECT 1 FROM data_ingestion_batches",
    "data_quality_issues": "SELECT 1 FROM data_quality_issues",
    "data_restatement_events": "SELECT 1 FROM data_restatement_events",
    "exchange_calendar_days": "SELECT 1 FROM exchange_calendar_days",
    "instrument_provider_symbols": "SELECT 1 FROM instrument_provider_symbols",
    "provider_capabilities": "SELECT 1 FROM provider_capabilities",
    "provider_health_state": "SELECT 1 FROM provider_health_state",
    "instruments (registry fields)": (
        "SELECT 1 FROM instruments WHERE trading_status <> 'ENABLED' OR disabled_reason IS NOT NULL"
        " OR contract_identifier IS NOT NULL OR expiry_date IS NOT NULL OR currency <> 'INR'"
    ),
}


def downgrade() -> None:
    # Dropping 0003's tables and columns would discard archived observations, provenance, job history, quality
    # issues, corporate actions, calendar entries and probe evidence. Refuse rather than lose any of it.
    conn = op.get_bind()
    holding = [name for name, query in _DOWNGRADE_GUARDS.items() if conn.execute(sa.text(f"SELECT EXISTS ({query})")).scalar()]
    if holding:
        raise RuntimeError(f"cannot downgrade 0003: market data would be discarded from {', '.join(holding)}")
    op.execute("""
        CREATE OR REPLACE FUNCTION candles_reject_immutable_update() RETURNS trigger AS $$
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
    op.drop_constraint(op.f('ck_candles_version_positive'), 'candles', type_='check')
    op.drop_constraint(op.f('ck_candles_only_finalized_archived'), 'candles', type_='check')
    op.drop_constraint(op.f('ck_instruments_equity_has_no_contract'), 'instruments', type_='check')
    op.drop_constraint(op.f('ck_instruments_disabled_requires_reason'), 'instruments', type_='check')
    op.drop_constraint(op.f('ck_instruments_futures_must_be_disabled'), 'instruments', type_='check')
    op.drop_column('instruments', 'disabled_reason')
    op.drop_column('instruments', 'trading_status')
    op.drop_column('instruments', 'expiry_date')
    op.drop_column('instruments', 'contract_identifier')
    op.drop_column('instruments', 'currency')
    op.drop_constraint(op.f('fk_corporate_actions_provider_id_data_providers'), 'corporate_actions', type_='foreignkey')
    op.drop_constraint(op.f('fk_corporate_actions_ingestion_batch_id_data_ingestion_batches'), 'corporate_actions', type_='foreignkey')
    op.drop_column('corporate_actions', 'provider_raw')
    op.drop_column('corporate_actions', 'observed_at')
    op.drop_column('corporate_actions', 'ingestion_batch_id')
    op.drop_column('corporate_actions', 'provider_id')
    op.drop_constraint(op.f('fk_candles_ingestion_batch_id_data_ingestion_batches'), 'candles', type_='foreignkey')
    op.drop_constraint('uq_candles_observation', 'candles', type_='unique')
    op.drop_index('uq_candles_canonical', table_name='candles', postgresql_where=sa.text('is_canonical'))
    op.drop_index(op.f('ix_candles_ingestion_batch_id'), table_name='candles')
    op.create_unique_constraint(op.f('uq_candles_identity'), 'candles', ['instrument_id', 'provider_id', 'timeframe', 'bar_start_ts'], postgresql_nulls_not_distinct=False)
    op.drop_column('candles', 'is_canonical')
    op.drop_column('candles', 'observation_version')
    op.drop_column('candles', 'source_metadata')
    op.drop_column('candles', 'ingestion_batch_id')
    op.drop_column('candles', 'provider_symbol')
    op.drop_index(op.f('ix_data_restatement_events_instrument_id'), table_name='data_restatement_events')
    op.drop_table('data_restatement_events')
    op.drop_index('ix_data_quality_issues_instrument_type', table_name='data_quality_issues')
    op.drop_index(op.f('ix_data_quality_issues_batch_id'), table_name='data_quality_issues')
    op.drop_table('data_quality_issues')
    op.drop_index(op.f('ix_data_ingestion_batches_job_id'), table_name='data_ingestion_batches')
    op.drop_index('ix_data_ingestion_batches_instrument_started', table_name='data_ingestion_batches')
    op.drop_table('data_ingestion_batches')
    op.drop_table('provider_health_state')
    op.drop_table('provider_capabilities')
    op.drop_table('instrument_provider_symbols')
    op.drop_index('uq_ingestion_jobs_single_active', table_name='data_ingestion_jobs', postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"))
    op.drop_index(op.f('ix_data_ingestion_jobs_requested_by_user_id'), table_name='data_ingestion_jobs')
    op.drop_table('data_ingestion_jobs')
    op.drop_table('exchange_calendar_days')
