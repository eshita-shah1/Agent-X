"""Phase 6 endpoints: the caller's trading journal.

Identity comes only from the authenticated user; another user's entry is 404 and a link to another user's record is
refused with the same 422 as a missing one. Automatic entries are created only by POST /journal/sync from the existing
trading records (idempotent) and keep their system facts; their notes are editable. Manual entries are the user's own.
Nothing here writes orders, fills, positions, the ledger, risk settings or agent state.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.explanations import get_clock, get_narrator, stored_response, today
from app.api.v1.schemas_journal import (
    AnalyticsNarrativeRequest,
    AnalyticsResponse,
    Deterministic,
    ExplanationResponse,
    JournalCreateRequest,
    JournalEntryDetail,
    JournalEntryResponse,
    JournalOverview,
    JournalPage,
    JournalUpdateRequest,
    Narrative,
    ReflectionAssistResponse,
    ReflectionRequest,
    ReflectionResponse,
    Section,
    SyncRequest,
    SyncResponse,
    TagCount,
)
from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import ExplanationSubject, JournalEntryType, JournalSourceEvent, NarrativeStatus, TradeOutcome
from app.db.models import AuditEvent, Instrument, JournalEntry, JournalReflection, User
from app.explanations.facts import Facts
from app.explanations.render import GENERATOR_VERSION
from app.explanations.service import ExplanationService
from app.journal import insights, sync as journal_sync
from app.journal.reflection import DRAFT_AUDIT_EVENT, reflection_questions, reflection_task
from app.journal.service import EntryNotFound, JournalError, JournalService, SearchFilters
from app.llm.narrator import NarrativeRequest, NarrativeResult, Narrator

router = APIRouter(prefix="/journal", tags=["journal"])

MAX_WINDOW_DAYS = 366


def _error(exc: JournalError) -> HTTPException:
    if isinstance(exc, EntryNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Journal entry not found")
    code = status.HTTP_409_CONFLICT if exc.code in (
        "AUTO_ENTRY_FACTS_IMMUTABLE", "AUTO_ENTRY_NOT_DELETABLE", "TRADE_NOT_CLOSED") else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, f"{exc.code}: {exc}")


def _window(first: date | None, last: date | None, today: date, default_days: int = 30) -> tuple[date, date]:
    last = last or today
    first = first or last - timedelta(days=default_days - 1)
    if first > last or (last - first).days >= MAX_WINDOW_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"date_from must not be after date_to and the window at most {MAX_WINDOW_DAYS} days")
    return first, last


def _symbols(db: Session, entries: list[JournalEntry]) -> dict[int, str]:
    ids = {e.instrument_id for e in entries if e.instrument_id is not None}
    return {i.id: i.symbol for i in db.scalars(select(Instrument).where(Instrument.id.in_(ids)))} if ids else {}


def _responses(svc: JournalService, entries: list[JournalEntry]) -> list[JournalEntryResponse]:
    tags = svc.tags_of([e.id for e in entries])
    symbols = _symbols(svc.db, entries)
    reflected = set(svc.db.scalars(select(JournalReflection.entry_id).where(
        JournalReflection.user_id == svc.user_id, JournalReflection.entry_id.in_([e.id for e in entries])))) if entries else set()
    return [JournalEntryResponse(
        **{k: getattr(e, k) for k in JournalEntryResponse.model_fields if hasattr(e, k)},
        symbol=symbols.get(e.instrument_id), tags=tags[e.id], has_reflection=e.id in reflected,
    ) for e in entries]


def _detail(svc: JournalService, entry: JournalEntry) -> JournalEntryDetail:
    base = _responses(svc, [entry])[0]
    reflection = svc.reflection(entry.id)
    return JournalEntryDetail(
        **base.model_dump(), reflection=ReflectionResponse.model_validate(reflection) if reflection else None,
        linked=svc.linked(entry), reflection_allowed=svc.closed_trade(entry) is not None,
    )


@router.get("/overview", response_model=JournalOverview)
def overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return JournalService(db, user.id).overview()


@router.get("/tags", response_model=list[TagCount])
def list_tags(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [TagCount(name=n, entries=c) for n, c in JournalService(db, user.id).tag_counts()]


@router.post("/sync", response_model=SyncResponse)
def sync_entries(body: SyncRequest | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                 clock: Clock = Depends(get_clock)):
    """Create the missing automatic entries for a window (default: the last 30 days) from the trading records."""
    body = body or SyncRequest()
    now = clock()
    first, last = _window(body.date_from, body.date_to, today(lambda: now))
    result = journal_sync.sync(db, user.id, first, last, now)
    db.commit()
    return SyncResponse(date_from=first, date_to=last, candidates=result["candidates"], created=result["created"],
                        created_by_event=result["created_by_event"])


@router.get("/analytics", response_model=AnalyticsResponse)
def get_analytics(date_from: date | None = None, date_to: date | None = None, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    first, last = _window(date_from, date_to, today(clock))
    facts, _texts = insights.analytics(db, user.id, first, last)
    p = ExplanationService.preview_of(ExplanationSubject.JOURNAL_INSIGHTS, f"{first}:{last}", Facts(facts, {}))
    return AnalyticsResponse(analytics=facts, deterministic=Deterministic(
        generator_version=GENERATOR_VERSION, text=p.text, sections=[Section(layer=a, text=b) for a, b in p.sections]))


@router.post("/analytics/narrative", response_model=ExplanationResponse, status_code=status.HTTP_201_CREATED)
def analytics_narrative(body: AnalyticsNarrativeRequest, response: Response, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
                        clock: Clock = Depends(get_clock), narrator: Narrator = Depends(get_narrator)):
    """An optional LLM narrative over the deterministic analytics (stored; rule-based when no provider is usable)."""
    first, last = _window(body.date_from, body.date_to, today(clock))
    facts, texts = insights.analytics(db, user.id, first, last)
    evidence = {"period": {"from": first.isoformat(), "to": last.isoformat()}, "source": "journal analytics"}
    svc = ExplanationService(db, settings, clock, narrator)
    p = svc.preview_of(ExplanationSubject.JOURNAL_INSIGHTS, f"{first}:{last}", Facts(facts, evidence))
    row, created = svc.store(user.id, p, True, [t for t in texts if t])
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return stored_response(row)


@router.get("", response_model=JournalPage)
def search(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    instrument_id: int | None = Query(default=None, gt=0),
    strategy_id: str | None = Query(default=None, max_length=32),
    date_from: date | None = None, date_to: date | None = None,
    outcome: TradeOutcome | None = None,
    tag: list[str] = Query(default=[], max_length=10),
    entry_type: JournalEntryType | None = None,
    source_event: JournalSourceEvent | None = None,
    order_id: str | None = Query(default=None, max_length=36),
    position_id: int | None = Query(default=None, gt=0),
    has_reflection: bool | None = None,
    sort: str = Query(default="occurred_at", pattern="^(occurred_at|created_at|updated_at|relevance)$"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        oid = uuid.UUID(order_id) if order_id else None
        svc = JournalService(db, user.id)
        filters = SearchFilters(q, instrument_id, strategy_id, date_from, date_to, outcome, tuple(tag), entry_type,
                                source_event, oid, position_id, has_reflection, sort, direction, limit, offset)
        items, total = svc.search(filters)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "order_id must be a UUID") from None
    except JournalError as exc:
        raise _error(exc) from None
    return JournalPage(items=_responses(svc, items), total=total, limit=limit, offset=offset)


@router.post("", response_model=JournalEntryDetail, status_code=status.HTTP_201_CREATED)
def create_entry(body: JournalCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                 clock: Clock = Depends(get_clock)):
    svc = JournalService(db, user.id)
    try:
        entry = svc.create_manual(body.model_dump(exclude={"tags"}), body.tags, clock())
    except JournalError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    return _detail(svc, entry)


@router.get("/{entry_id}", response_model=JournalEntryDetail)
def get_entry(entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = JournalService(db, user.id)
    try:
        return _detail(svc, svc.get(entry_id))
    except JournalError as exc:
        raise _error(exc) from None


@router.patch("/{entry_id}", response_model=JournalEntryDetail)
def update_entry(entry_id: int, body: JournalUpdateRequest, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    svc = JournalService(db, user.id)
    patch = body.model_dump(exclude_unset=True, exclude={"tags"})
    try:
        entry = svc.update(entry_id, patch, body.tags, clock())
    except JournalError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    return _detail(svc, entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        JournalService(db, user.id).delete(entry_id)
    except JournalError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{entry_id}/reflection", response_model=ReflectionResponse, status_code=status.HTTP_201_CREATED)
def save_reflection(entry_id: int, body: ReflectionRequest, response: Response, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    """Create or replace the reflection on a closed trade. Saved only by this explicit request."""
    try:
        row, created = JournalService(db, user.id).save_reflection(entry_id, body.model_dump(), clock())
    except JournalError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return row


@router.post("/{entry_id}/reflection/assist", response_model=ReflectionAssistResponse)
def assist_reflection(entry_id: int, narrative: bool = True, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
                      clock: Clock = Depends(get_clock), narrator: Narrator = Depends(get_narrator)):
    """Suggested reflection questions (deterministic) and an optional LLM draft summary. Nothing is saved."""
    svc = JournalService(db, user.id)
    try:
        entry = svc.get(entry_id)
    except JournalError as exc:
        raise _error(exc) from None
    trade = svc.closed_trade(entry)
    if trade is None:
        raise _error(JournalError("TRADE_NOT_CLOSED", "reflection assistance needs an entry linked to a closed trade"))
    linked = svc.linked(entry)
    questions = reflection_questions(entry, trade, linked)
    draft = None
    if narrative and narrator.enabled:
        explanations = ExplanationService(db, settings, clock, narrator)
        if explanations.llm_calls_last_hour(user.id) >= settings.llm_max_requests_per_user_per_hour:
            result = NarrativeResult(NarrativeStatus.RATE_LIMITED, error="LLM_HOURLY_BUDGET_USED")
        else:
            facts = {"kind": "TRADE_REFLECTION", "paper_trading_only": True, "entry_title": entry.title,
                     "source_event": entry.source_event.value if entry.source_event else None,
                     "trade": linked["trade"], "fills": linked["fills"], "instrument": linked["instrument"]}
            result = narrator.narrate(NarrativeRequest(reflection_task(), facts, "\n".join(questions)))
            # counted towards the hourly LLM budget; the draft itself is not stored
            db.add(AuditEvent(user_id=user.id, event_type=DRAFT_AUDIT_EVENT, details={
                "entry_id": entry.id, "provider": result.provider, "model": result.model,
                "status": result.status.value, "error": result.error}))
            db.commit()
        draft = Narrative(status=result.status, text=result.narrative, caveats=result.caveats, provider=result.provider,
                          model=result.model, error=result.error, latency_ms=result.latency_ms)
    elif narrative:
        draft = Narrative(status=NarrativeStatus.DISABLED, text=None, caveats=[], provider=None, model=None,
                          error=(settings.llm_config_problems() or ["DISABLED"])[0][:64], latency_ms=None)
    db.rollback()
    return ReflectionAssistResponse(questions=questions, draft=draft)
