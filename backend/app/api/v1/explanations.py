"""Phase 6 endpoints: explanations of the caller's own trading records (paper trading only).

Read-only towards trading: these routes read decisions, orders, fills, risk decisions and events, and write only
`trade_explanations`. No route here reaches order submission, risk settings, positions or the agent lifecycle. GET
routes return live previews (deterministic, never stored, no LLM); POST /generate stores the explanation and, if a
provider is configured, an LLM narrative validated against the facts. Another user's record is always 404.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.schemas_journal import (
    Deterministic,
    ExplanationResponse,
    GenerateRequest,
    Narrative,
    NarratorStatusResponse,
    Section,
)
from app.core.clock import Clock, utc_now
from app.core.config import Settings
from app.db.enums import ExplanationSubject
from app.db.models import TradeExplanation, User
from app.db.scoping import owned_select
from app.explanations.facts import IST
from app.explanations.render import GENERATOR_VERSION
from app.explanations.service import ExplanationService, Preview, SubjectNotFound
from app.llm.narrator import Narrator

router = APIRouter(prefix="/explanations", tags=["explanations"])


def get_clock(request: Request) -> Clock:
    # Same hook as the trading router's, defined here so no Phase 6 route imports the order pipeline.
    return getattr(request.app.state, "clock", utc_now)


def today(clock: Clock) -> date:
    return clock().astimezone(IST).date()


def get_narrator(request: Request, settings: Settings = Depends(get_settings_dep)) -> Narrator:
    # Tests may install a fake provider on app.state; production builds the configured one (or none).
    return Narrator(settings, getattr(request.app.state, "llm_provider", None))


def get_explanations(db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep),
                     clock: Clock = Depends(get_clock), narrator: Narrator = Depends(get_narrator)) -> ExplanationService:
    return ExplanationService(db, settings, clock, narrator)


def _sections(text: str) -> list[Section]:
    out = []
    for line in text.splitlines():
        layer, _, body = line.partition(": ")
        out.append(Section(layer=layer, text=body))
    return out


def preview_response(p: Preview) -> ExplanationResponse:
    return ExplanationResponse(
        id=None, persisted=False, subject_type=p.subject_type, subject_key=p.subject_key, verified_facts=p.facts,
        evidence=p.evidence, facts_fingerprint=p.fingerprint,
        deterministic=Deterministic(generator_version=GENERATOR_VERSION, text=p.text,
                                    sections=[Section(layer=a, text=b) for a, b in p.sections]),
        narrative=None, created_at=None,
    )


def stored_response(row: TradeExplanation) -> ExplanationResponse:
    return ExplanationResponse(
        id=row.id, persisted=True, subject_type=row.subject_type, subject_key=row.subject_key, verified_facts=row.facts,
        evidence=row.evidence, facts_fingerprint=row.facts_fingerprint,
        deterministic=Deterministic(generator_version=row.generator_version, text=row.deterministic_text,
                                    sections=_sections(row.deterministic_text)),
        narrative=Narrative(status=row.narrative_status, text=row.narrative, caveats=row.narrative_caveats or [],
                            provider=row.provider, model=row.model, error=row.narrative_error, latency_ms=row.latency_ms),
        created_at=row.created_at,
    )


def _preview(svc: ExplanationService, user: User, subject: ExplanationSubject, subject_id: str) -> ExplanationResponse:
    try:
        return preview_response(svc.preview(user.id, subject, subject_id))
    except SubjectNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found") from None


@router.get("/provider", response_model=NarratorStatusResponse)
def provider_status(user: User = Depends(get_current_user), svc: ExplanationService = Depends(get_explanations),
                    settings: Settings = Depends(get_settings_dep)):
    """Which narrator is active. Never returns credentials."""
    return NarratorStatusResponse(**svc.narrator.status(), llm_calls_last_hour=svc.llm_calls_last_hour(user.id),
                                  llm_hourly_budget=settings.llm_max_requests_per_user_per_hour)


@router.get("/daily-summary", response_model=ExplanationResponse)
def daily_summary(session_date: date | None = Query(default=None, description="exchange-local (IST) date; default today"),
                  user: User = Depends(get_current_user), svc: ExplanationService = Depends(get_explanations)):
    d = session_date or today(svc.clock)
    return _preview(svc, user, ExplanationSubject.DAILY_SUMMARY, d.isoformat())


@router.get("/decisions/{decision_id}", response_model=ExplanationResponse)
def explain_decision(decision_id: int, user: User = Depends(get_current_user),
                     svc: ExplanationService = Depends(get_explanations)):
    return _preview(svc, user, ExplanationSubject.AGENT_DECISION, str(decision_id))


@router.get("/orders/{order_id}", response_model=ExplanationResponse)
def explain_order(order_id: str, user: User = Depends(get_current_user), svc: ExplanationService = Depends(get_explanations)):
    return _preview(svc, user, ExplanationSubject.ORDER, order_id)


@router.get("/events/{event_id}", response_model=ExplanationResponse)
def explain_event(event_id: int, user: User = Depends(get_current_user), svc: ExplanationService = Depends(get_explanations)):
    return _preview(svc, user, ExplanationSubject.AGENT_EVENT, str(event_id))


@router.post("/generate", response_model=ExplanationResponse, status_code=status.HTTP_201_CREATED)
def generate(body: GenerateRequest, response: Response, user: User = Depends(get_current_user),
             svc: ExplanationService = Depends(get_explanations)):
    """Stores the explanation (idempotent: the same facts and narrator return the stored row with 200)."""
    if body.subject_type is ExplanationSubject.JOURNAL_INSIGHTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "use POST /api/v1/journal/analytics/narrative")
    try:
        row, created = svc.generate(user.id, body.subject_type, body.subject_id, body.narrative)
    except SubjectNotFound:
        svc.db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found") from None
    svc.db.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return stored_response(row)


@router.get("", response_model=list[ExplanationResponse])
def list_explanations(subject_type: ExplanationSubject | None = None, subject_key: str | None = Query(default=None, max_length=64),
                      before_id: int | None = Query(default=None, gt=0), limit: int = Query(default=20, ge=1, le=100),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = owned_select(TradeExplanation, user.id)
    if subject_type is not None:
        q = q.where(TradeExplanation.subject_type == subject_type)
    if subject_key is not None:
        q = q.where(TradeExplanation.subject_key == subject_key)
    if before_id is not None:
        q = q.where(TradeExplanation.id < before_id)
    return [stored_response(r) for r in db.scalars(q.order_by(TradeExplanation.id.desc()).limit(limit))]


@router.get("/{explanation_id}", response_model=ExplanationResponse)
def get_explanation(explanation_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.scalar(owned_select(TradeExplanation, user.id).where(TradeExplanation.id == explanation_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Explanation not found")
    return stored_response(row)
