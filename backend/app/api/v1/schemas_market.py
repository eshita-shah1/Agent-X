import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderSymbolOut(BaseModel):
    provider: str
    provider_symbol: str
    verification_status: str


class InstrumentOut(BaseModel):
    id: int
    symbol: str
    exchange: str
    instrument_type: str
    name: str | None
    currency: str
    timezone: str
    tick_size: Decimal | None
    lot_size: int | None
    is_active: bool
    trading_status: str
    disabled_reason: str | None
    contract_identifier: str | None
    expiry_date: date | None
    provider_symbols: list[ProviderSymbolOut]


class CandleOut(BaseModel):
    candle_id: int
    series: str
    timeframe: str
    bar_start_ts: datetime
    bar_end_ts: datetime
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    provider: str | None
    provider_symbol: str
    price_basis: str
    observation_version: int
    is_canonical: bool
    is_final: bool  # always true: forming bars are never archived or served
    observed_at: datetime
    ingested_at: datetime
    ingestion_batch_id: int
    adjustment_method: str | None = None
    adjustment_factor: Decimal | None = None
    corporate_action_set_version: str | None = None


class CandlePage(BaseModel):
    items: list[CandleOut]
    count: int
    next_cursor: datetime | None = Field(None, description="pass as `after` to fetch the next page")
    price_basis_note: str = (
        "price_basis UNVERIFIED_PROVIDER_NATIVE means provider-native values that have not been verified as "
        "as-traded. Yahoo history was observed to be split-adjusted. No price here is classified as execution-ready; "
        "the execution-price source is decided in Phase 4."
    )


class BackfillRequest(BaseModel):
    days: int = Field(60, ge=1, le=60, description="calendar days back; capped by the approved 60-day target and the provider's limit")
    instrument_ids: list[int] | None = Field(None, description="default: every enabled equity with a provider mapping")


class IncrementalRequest(BaseModel):
    instrument_ids: list[int] | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: str
    timeframe: str
    parameters: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    error_message: str | None
    summary: dict[str, Any]


class JobPage(BaseModel):
    items: list[JobOut]
    count: int
    limit: int
    offset: int
