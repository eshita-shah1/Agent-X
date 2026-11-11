"""Global market-data tables (no user_id: candles are shared across users).

Raw/adjusted separation is structural:
  * `candles` holds provider-native OHLCV exactly as ingested. A database trigger (see the
    initial migration) rejects any UPDATE of its price, volume, time or identity columns.
  * `adjusted_candles` holds adjusted values in a separate table, with method, factor and
    source, and can never overwrite a `candles` row.
Phase 2 adds provenance (provider symbol, ingestion batch, source metadata), observation versioning for
restatements, and the instrument registry safety rails (futures stay disabled).
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.enums import (
    AdjustmentMethod,
    CorporateActionType,
    InstrumentType,
    PriceBasis,
    Timeframe,
    TradingStatus,
)


def enum_type(cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR with a CHECK constraint (not a native PG enum) so adding a value later
    # is an ordinary migration. The enum *value* is what is stored.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


PRICE = Numeric(18, 6)


class DataProvider(Base):
    __tablename__ = "data_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Instrument(Base):
    """An instrument in the registry. For futures, `contract_identifier` and `expiry_date` identify a specific
    contract once genuine contract-specific data exists; until then futures rows are disabled placeholders.
    (A future phase supporting several expiries per underlying will relax uq_instruments_identity.)"""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "instrument_type", name="uq_instruments_identity"),
        # Safety rails: futures cannot be enabled in Phase 2, and a disabled instrument must say why.
        CheckConstraint("instrument_type <> 'FUTURE' OR trading_status = 'DISABLED'", name="futures_must_be_disabled"),
        CheckConstraint("trading_status = 'ENABLED' OR disabled_reason IS NOT NULL", name="disabled_requires_reason"),
        CheckConstraint(
            "instrument_type <> 'EQUITY' OR (expiry_date IS NULL AND contract_identifier IS NULL)",
            name="equity_has_no_contract",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(enum_type(InstrumentType, "instrument_type"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    # Contract metadata is sourced later; nothing is defaulted or invented here.
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_identifier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    trading_status: Mapped[TradingStatus] = mapped_column(
        enum_type(TradingStatus, "trading_status"), nullable=False, server_default=TradingStatus.ENABLED.value
    )
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


_OHLC_CHECKS = (
    "{p}high >= {p}low",
    "{p}high >= {p}open",
    "{p}high >= {p}close",
    "{p}low <= {p}open",
    "{p}low <= {p}close",
    "{p}low > 0",
)


class Candle(Base):
    """Provider-native OHLCV as ingested. Immutable (enforced by trigger).

    Versioning: the first observation of a bar is version 1 and is the *canonical* version. If the provider
    later reports different values for an already-archived bar, the new observation is stored as an additional,
    non-canonical version (and a DataRestatementEvent is recorded); archived observations are never overwritten.
    Approved policy: the first observation stays canonical; later versions never become canonical.
    """

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider_id", "timeframe", "bar_start_ts", "observation_version", name="uq_candles_observation"
        ),
        Index(
            "uq_candles_canonical",
            "instrument_id",
            "provider_id",
            "timeframe",
            "bar_start_ts",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
        CheckConstraint(" AND ".join(c.format(p="") for c in _OHLC_CHECKS), name="ohlc_valid"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("bar_end_ts > bar_start_ts", name="bar_interval_valid"),
        CheckConstraint("is_final", name="only_finalized_archived"),
        CheckConstraint("observation_version >= 1", name="version_positive"),
        # A verified basis must name what verified it; unverified is the only label that needs no evidence.
        CheckConstraint(
            "price_basis = 'UNVERIFIED_PROVIDER_NATIVE' OR basis_verified_by IS NOT NULL",
            name="verified_basis_has_evidence",
        ),
        Index("ix_candles_instrument_timeframe_start", "instrument_id", "timeframe", "bar_start_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False)
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    timeframe: Mapped[Timeframe] = mapped_column(enum_type(Timeframe, "timeframe"), nullable=False)
    bar_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # UTC instant
    bar_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)  # exchange-local trading date
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # when Agent-X received it
    # Never "raw"/"as traded" until verified. The two columns below are the only mutable ones.
    price_basis: Mapped[PriceBasis] = mapped_column(
        enum_type(PriceBasis, "price_basis"),
        nullable=False,
        server_default=PriceBasis.UNVERIFIED_PROVIDER_NATIVE.value,
    )
    basis_verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Provenance (Phase 2)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    ingestion_batch_id: Mapped[int] = mapped_column(
        ForeignKey("data_ingestion_batches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    observation_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class AdjustedCandle(Base):
    """Adjusted prices derived from a candle. Stored apart from, and never replacing, raw data."""

    __tablename__ = "adjusted_candles"
    __table_args__ = (
        UniqueConstraint(
            "candle_id", "adjustment_method", "corporate_action_set_version", name="uq_adjusted_candles_identity"
        ),
        CheckConstraint(" AND ".join(c.format(p="adj_") for c in _OHLC_CHECKS), name="adj_ohlc_valid"),
        CheckConstraint("adjustment_factor > 0", name="factor_positive"),
        CheckConstraint("adj_volume IS NULL OR adj_volume >= 0", name="adj_volume_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candle_id: Mapped[int] = mapped_column(
        ForeignKey("candles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    adjustment_method: Mapped[AdjustmentMethod] = mapped_column(enum_type(AdjustmentMethod, "adjustment_method"), nullable=False)
    adjustment_source: Mapped[str] = mapped_column(String(128), nullable=False)
    corporate_action_set_version: Mapped[str] = mapped_column(String(64), nullable=False)
    adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    adj_open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    adj_high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    adj_low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    adj_close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    adj_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "action_type", "ex_date", "source", name="uq_corporate_actions_identity"),
        CheckConstraint(
            "(action_type NOT IN ('SPLIT','BONUS')) OR "
            "(ratio_numerator IS NOT NULL AND ratio_denominator IS NOT NULL "
            "AND ratio_numerator > 0 AND ratio_denominator > 0)",
            name="ratio_required_for_split_bonus",
        ),
        CheckConstraint(
            "(action_type <> 'DIVIDEND') OR (cash_amount IS NOT NULL AND cash_amount >= 0)",
            name="cash_required_for_dividend",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action_type: Mapped[CorporateActionType] = mapped_column(enum_type(CorporateActionType, "corporate_action_type"), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    ratio_numerator: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ratio_denominator: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cash_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Provenance (Phase 2). Split/bonus ratio is "new shares : old shares" as reported by the provider
    # (e.g. 2:1 for a 1:1 bonus or a 2-for-1 split); the provider does not distinguish the two.
    provider_id: Mapped[int] = mapped_column(ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False)
    ingestion_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_ingestion_batches.id", ondelete="RESTRICT"), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
