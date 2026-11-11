from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_INSTRUMENTS = 20  # operational bound per run
MAX_CANDIDATES = 10


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(min_length=1, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)  # overrides of the approved defaults; times as "HH:MM"
    priority: int | None = Field(default=None, ge=1)  # required in REGIME_PRIORITY mode, absent in PINNED mode


class CostModelIn(BaseModel):
    """All zero by default (the run is then labelled GROSS_OF_COSTS). Send decimals as strings to avoid float."""

    model_config = ConfigDict(extra="forbid")

    fee_per_order: Decimal = Field(default=Decimal(0), ge=0)
    fee_bps_per_side: Decimal = Field(default=Decimal(0), ge=0)
    sell_side_bps: Decimal = Field(default=Decimal(0), ge=0)
    slippage_bps: Decimal = Field(default=Decimal(0), ge=0, lt=10000)


class BacktestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_ids: list[int] = Field(min_length=1, max_length=MAX_INSTRUMENTS)
    start: date
    end: date
    mode: Literal["REGIME_PRIORITY", "PINNED"]
    candidates: list[CandidateIn] = Field(min_length=1, max_length=MAX_CANDIDATES)
    # Default: the virtual account's initial capital (read only). Each instrument receives the full capital.
    capital: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    cost_model: CostModelIn = Field(default_factory=CostModelIn)
    warmup_sessions: int = Field(default=2, ge=0, le=20)

    @field_validator("instrument_ids")
    @classmethod
    def _unique(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("instrument_ids must be unique")
        return v


class BacktestRunOut(BaseModel):
    id: uuid.UUID
    status: str
    instrument_ids: list[int]
    requested_start: date
    requested_end: date
    selection_mode: str
    capital: Decimal
    capital_source: str
    cost_basis: str
    price_basis: str
    engine_version: str
    fill_model: str
    sizing_model: str
    config_fingerprint: str
    data_fingerprint: str | None
    result_fingerprint: str | None
    request: dict[str, Any]
    disclosures: list[str]
    summary: dict[str, Any]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None


class BacktestRunPage(BaseModel):
    items: list[BacktestRunOut]
    count: int
    limit: int
    offset: int


class ItemPage(BaseModel):
    """Result rows; decimals are exact strings, timestamps UTC ISO-8601."""

    items: list[dict[str, Any]]
    count: int
    limit: int
    offset: int
