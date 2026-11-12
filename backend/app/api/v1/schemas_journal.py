"""Request/response schemas for the Phase 6 explanation and journal endpoints. Database models are never returned
directly. Text inputs are bounded; unknown fields are refused."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.db.enums import ExplanationSubject, JournalEntryType, JournalSourceEvent, NarrativeStatus, TradeOutcome

Note = str | None
NOTE = Field(default=None, max_length=10_000)
NARRATIVE_LABEL = ("AI-generated interpretation of the verified facts. It is not a verified trading fact, not advice and "
                   "not an instruction; the facts and the rule-based explanation are authoritative.")

# ---- explanations ------------------------------------------------------------------------------------------------


class Section(BaseModel):
    layer: str
    text: str


class Deterministic(BaseModel):
    kind: str = "RULE_BASED"
    generator_version: str
    text: str
    sections: list[Section]


class Narrative(BaseModel):
    status: NarrativeStatus
    text: str | None
    caveats: list[str]
    provider: str | None
    model: str | None
    error: str | None
    latency_ms: int | None
    label: str = NARRATIVE_LABEL


class ExplanationResponse(BaseModel):
    id: int | None  # None for a live preview (not stored)
    persisted: bool
    subject_type: ExplanationSubject
    subject_key: str
    verified_facts: dict[str, Any]  # copied from stored trading records
    evidence: dict[str, Any]  # identifiers of every record used
    facts_fingerprint: str
    deterministic: Deterministic
    narrative: Narrative | None
    created_at: datetime | None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: ExplanationSubject
    subject_id: str = Field(min_length=1, max_length=64)  # decision/event id, order UUID or session date
    narrative: bool = True  # also ask the configured LLM (if any) for a narrative


class NarratorStatusResponse(BaseModel):
    enabled: bool
    provider: str
    model: str | None
    configuration_problems: list[str]
    includes_journal_notes: bool
    fallback: str
    llm_calls_last_hour: int
    llm_hourly_budget: int


# ---- journal -----------------------------------------------------------------------------------------------------


class _Notes(BaseModel):
    notes: Note = NOTE
    entry_thesis: Note = NOTE
    exit_thesis: Note = NOTE
    market_observations: Note = NOTE
    went_well: Note = NOTE
    went_wrong: Note = NOTE
    lessons: Note = NOTE
    emotional_notes: Note = NOTE
    rating: int | None = Field(default=None, ge=1, le=5)
    confidence: int | None = Field(default=None, ge=1, le=5)


class _Links(BaseModel):
    instrument_id: int | None = Field(default=None, gt=0)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=32)
    order_id: uuid.UUID | None = None
    fill_id: int | None = Field(default=None, gt=0)
    position_id: int | None = Field(default=None, gt=0)
    agent_decision_id: int | None = Field(default=None, gt=0)
    agent_event_id: int | None = Field(default=None, gt=0)
    config_version_id: int | None = Field(default=None, gt=0)
    backtest_run_id: uuid.UUID | None = None


class JournalCreateRequest(_Notes, _Links):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    occurred_at: AwareDatetime | None = None  # defaults to now
    tags: list[str] = Field(default_factory=list, max_length=20)


class JournalUpdateRequest(_Notes, _Links):
    """Only the fields sent are changed. On an automatic entry only the notes, rating, confidence and tags may change."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    occurred_at: AwareDatetime | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class ReflectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_thesis: Note = NOTE
    followed_strategy_rules: bool | None = None
    risk_limits_respected: bool | None = None
    exit_followed_strategy: bool | None = None
    mistakes: Note = NOTE
    lessons: Note = NOTE
    future_improvements: Note = NOTE


class ReflectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    original_thesis: str | None
    followed_strategy_rules: bool | None
    risk_limits_respected: bool | None
    exit_followed_strategy: bool | None
    mistakes: str | None
    lessons: str | None
    future_improvements: str | None
    created_at: datetime
    updated_at: datetime


class JournalEntryResponse(BaseModel):
    id: int
    entry_type: JournalEntryType
    source_event: JournalSourceEvent | None
    title: str
    occurred_at: datetime
    session_date: date
    outcome: TradeOutcome
    instrument_id: int | None
    symbol: str | None
    strategy_id: str | None
    order_id: uuid.UUID | None
    fill_id: int | None
    position_id: int | None
    agent_decision_id: int | None
    agent_event_id: int | None
    config_version_id: int | None
    backtest_run_id: uuid.UUID | None
    system_facts: dict[str, Any]  # immutable for automatic entries; identifiers and labels only
    notes: str | None
    entry_thesis: str | None
    exit_thesis: str | None
    market_observations: str | None
    went_well: str | None
    went_wrong: str | None
    lessons: str | None
    emotional_notes: str | None
    rating: int | None
    confidence: int | None
    tags: list[str]
    has_reflection: bool
    created_at: datetime
    updated_at: datetime


class JournalEntryDetail(JournalEntryResponse):
    reflection: ReflectionResponse | None
    linked: dict[str, Any]  # read live from the authoritative records
    reflection_allowed: bool


class JournalPage(BaseModel):
    items: list[JournalEntryResponse]
    total: int
    limit: int
    offset: int


class JournalOverview(BaseModel):
    total: int
    manual: int
    automatic: int
    reflections: int
    latest_occurred_at: datetime | None


class TagCount(BaseModel):
    name: str
    entries: int


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None


class SyncResponse(BaseModel):
    date_from: date
    date_to: date
    candidates: int
    created: int
    created_by_event: dict[str, int]


class ReflectionAssistResponse(BaseModel):
    questions: list[str]  # deterministic prompts derived from the trade's facts
    draft: Narrative | None  # optional LLM draft summary: NOT saved; the user decides what to write
    saved: bool = False


class AnalyticsResponse(BaseModel):
    analytics: dict[str, Any]
    deterministic: Deterministic


class AnalyticsNarrativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
