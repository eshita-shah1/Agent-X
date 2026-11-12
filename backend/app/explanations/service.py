"""Explanations: preview (live, read-only) and generate (stored, optionally with an LLM narrative).

The service reads authoritative records and writes only `trade_explanations`. It imports no order, exchange-mutating,
risk-setting or agent-lifecycle service: an explanation can never change trading state, whatever the narrator returns.

Generation is idempotent: the same facts (sha256 of their canonical JSON) explained by the same narrator are stored
once; asking again returns the stored row. Failed narratives are stored too (as an audit of LLM calls) and count
towards the per-user hourly LLM budget, beyond which explanations stay rule-based (RATE_LIMITED).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.db.enums import ExplanationSubject, NarrativeStatus
from app.db.models import AuditEvent, TradeExplanation, VirtualAccount
from app.db.scoping import owned_select
from app.exchange.portfolio import PortfolioReader
from app.exchange.pricing import build_price_gate
from app.explanations import facts as F
from app.explanations.render import GENERATOR_VERSION, Sections, as_text, render
from app.llm.narrator import NarrativeRequest, NarrativeResult, Narrator

# Unstored LLM calls (reflection drafts) are recorded as audit events so they count towards the hourly budget.
DRAFT_AUDIT_EVENT = "llm.reflection_draft"

TASKS = {
    ExplanationSubject.AGENT_DECISION: "Explain this agent decision: why the strategy signalled (if it did), what the "
                                       "agent decided and why, and what happened to any order.",
    ExplanationSubject.ORDER: "Explain this order: how it was created, the risk decision, its execution, fees and the "
                              "outcome of its trade.",
    ExplanationSubject.AGENT_EVENT: "Explain this agent event and what it means for the agent's behaviour.",
    ExplanationSubject.DAILY_SUMMARY: "Summarise this paper-trading session. Distinguish signals, orders and closed "
                                      "trades, and closed-trade P&L from unrealized P&L.",
    ExplanationSubject.JOURNAL_INSIGHTS: "Describe the patterns in these journal analytics, stating sample sizes. Do "
                                         "not infer causation and do not recommend changes to risk or strategies.",
}


class SubjectNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Preview:
    subject_type: ExplanationSubject
    subject_key: str
    facts: dict
    evidence: dict
    sections: Sections
    fingerprint: str

    @property
    def text(self) -> str:
        return as_text(self.sections)


def fingerprint(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class ExplanationService:
    def __init__(self, db: Session, settings: Settings, clock: Clock, narrator: Narrator | None = None):
        self.db, self.settings, self.clock = db, settings, clock
        self.narrator = narrator or Narrator(settings)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def _marks(self, user_id: uuid.UUID):
        account = self.db.scalar(owned_select(VirtualAccount, user_id))
        reader = PortfolioReader(self.db, build_price_gate(self.settings, self.db, self.clock))
        valuation = reader.valuation(account) if account else None
        by_position = {v.position.id: v for v in valuation.positions} if valuation else {}

        def marks(position):
            v = by_position.get(position.id)
            if v is None:
                return None, None, "position not valued"
            if v.mark is None:
                rejection = v.price_check.rejection.value if v.price_check.rejection else "UNAVAILABLE"
                return None, None, f"{rejection}: {v.price_check.detail}"
            return v.unrealized_pnl, v.mark, None

        return marks

    def facts(self, user_id: uuid.UUID, subject_type: ExplanationSubject, subject_id: str) -> tuple[str, F.Facts]:
        try:
            if subject_type is ExplanationSubject.AGENT_DECISION:
                key, found = str(int(subject_id)), F.decision_facts(self.db, user_id, int(subject_id))
            elif subject_type is ExplanationSubject.ORDER:
                oid = uuid.UUID(subject_id)
                key, found = str(oid), F.order_facts(self.db, user_id, oid, self._marks(user_id))
            elif subject_type is ExplanationSubject.AGENT_EVENT:
                key, found = str(int(subject_id)), F.event_facts(self.db, user_id, int(subject_id))
            elif subject_type is ExplanationSubject.DAILY_SUMMARY:
                d = date.fromisoformat(subject_id)
                key, found = d.isoformat(), F.daily_facts(self.db, user_id, d)
            else:
                raise SubjectNotFound(subject_type.value)
        except (ValueError, OverflowError):
            raise SubjectNotFound(subject_id) from None
        if found is None:
            raise SubjectNotFound(subject_id)
        return key, found

    def preview(self, user_id: uuid.UUID, subject_type: ExplanationSubject, subject_id: str) -> Preview:
        key, f = self.facts(user_id, subject_type, subject_id)
        return self.preview_of(subject_type, key, f)

    @staticmethod
    def preview_of(subject_type: ExplanationSubject, key: str, f: F.Facts) -> Preview:
        return Preview(subject_type, key, f.facts, f.evidence, render(subject_type.value, f.facts), fingerprint(f.facts))

    # ---- generation -----------------------------------------------------------------------------------------------

    def llm_calls_last_hour(self, user_id: uuid.UUID) -> int:
        """Stored LLM attempts (any status) plus unstored reflection drafts (audit events) in the last hour."""
        since = self.clock() - timedelta(hours=1)
        stored = self.db.scalar(select(func.count()).select_from(TradeExplanation).where(
            TradeExplanation.user_id == user_id, TradeExplanation.provider.is_not(None),
            TradeExplanation.created_at >= since))
        drafts = self.db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.user_id == user_id, AuditEvent.event_type == DRAFT_AUDIT_EVENT, AuditEvent.created_at >= since))
        return stored + drafts

    def generate(self, user_id: uuid.UUID, subject_type: ExplanationSubject, subject_id: str, narrative: bool = True,
                 untrusted_text: list[str] | None = None) -> tuple[TradeExplanation, bool]:
        key, f = self.facts(user_id, subject_type, subject_id)
        return self.store(user_id, self.preview_of(subject_type, key, f), narrative, untrusted_text)

    def store(self, user_id: uuid.UUID, p: Preview, narrative: bool,
              untrusted_text: list[str] | None = None) -> tuple[TradeExplanation, bool]:
        """Returns (row, created). Never raises because of the narrator."""
        if not narrative:
            result = NarrativeResult(NarrativeStatus.NOT_REQUESTED)
        elif not self.narrator.enabled:
            result = self.narrator.narrate(NarrativeRequest("", {}, ""))  # DISABLED with the configuration reason
        else:
            planned = f"{self.narrator.provider.name}:{self.narrator.provider.model}"
            existing = self._existing(user_id, p, planned)
            if existing is not None:
                return existing, False
            if self.llm_calls_last_hour(user_id) >= self.settings.llm_max_requests_per_user_per_hour:
                result = NarrativeResult(NarrativeStatus.RATE_LIMITED, error="LLM_HOURLY_BUDGET_USED")
            else:
                result = self.narrator.narrate(NarrativeRequest(TASKS[p.subject_type], p.facts, p.text,
                                                                untrusted_text or []))
        existing = self._existing(user_id, p, result.narrator) if result.status in (
            NarrativeStatus.NOT_REQUESTED, NarrativeStatus.DISABLED, NarrativeStatus.GENERATED) else None
        if existing is not None:
            return existing, False
        row = TradeExplanation(
            user_id=user_id, subject_type=p.subject_type, subject_key=p.subject_key,
            agent_decision_id=int(p.subject_key) if p.subject_type is ExplanationSubject.AGENT_DECISION else None,
            order_id=uuid.UUID(p.subject_key) if p.subject_type is ExplanationSubject.ORDER else None,
            agent_event_id=int(p.subject_key) if p.subject_type is ExplanationSubject.AGENT_EVENT else None,
            session_date=date.fromisoformat(p.subject_key) if p.subject_type is ExplanationSubject.DAILY_SUMMARY else None,
            facts=p.facts, evidence=p.evidence, facts_fingerprint=p.fingerprint, generator_version=GENERATOR_VERSION,
            deterministic_text=p.text, narrator=result.narrator, narrative_status=result.status,
            narrative=result.narrative, narrative_caveats=result.caveats, narrative_error=result.error,
            provider=result.provider, model=result.model, latency_ms=result.latency_ms, created_at=self.clock(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:  # a concurrent request stored the same explanation first
            existing = self._existing(user_id, p, result.narrator)
            if existing is None:
                raise
            return existing, False
        return row, True

    def _existing(self, user_id: uuid.UUID, p: Preview, narrator: str) -> TradeExplanation | None:
        return self.db.scalar(owned_select(TradeExplanation, user_id).where(
            TradeExplanation.subject_type == p.subject_type, TradeExplanation.subject_key == p.subject_key,
            TradeExplanation.facts_fingerprint == p.fingerprint, TradeExplanation.narrator == narrator,
            TradeExplanation.narrative_status.in_(
                (NarrativeStatus.NOT_REQUESTED, NarrativeStatus.DISABLED, NarrativeStatus.GENERATED)),
        ))
