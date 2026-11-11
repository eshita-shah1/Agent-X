"""Deterministic regime classification and strategy selection (Phase 3, Step 3).

Regime from the approved ADX(14) at the 09:30 IST checkpoint; candidates filtered by regime family, warm-up and an
explicit priority (or a pinned configuration); the decision is locked per instrument per session and fully
explained. No outcome or result metrics are inputs: selection never learns from past trading (decision D15).
"""

from app.strategies.selection.candidates import (
    ELIGIBLE_FAMILIES,
    Candidate,
    CandidateEvaluation,
    CandidateOutcome,
    configuration_problem,
    required_bars,
)
from app.strategies.selection.policy import PinnedPolicy, Policy, RegimePriorityPolicy, SelectionMode
from app.strategies.selection.regime import Regime, RegimeConfig, RegimeReason, RegimeResult, RegimeTracker, classify
from app.strategies.selection.selector import SelectionOutcome, SelectionReason, SelectionStatus, WarmupContext, select
from app.strategies.selection.session import InstrumentSelector, SelectionBook, SelectionDecision

__all__ = [
    "ELIGIBLE_FAMILIES",
    "Candidate",
    "CandidateEvaluation",
    "CandidateOutcome",
    "InstrumentSelector",
    "PinnedPolicy",
    "Policy",
    "Regime",
    "RegimeConfig",
    "RegimePriorityPolicy",
    "RegimeReason",
    "RegimeResult",
    "RegimeTracker",
    "SelectionBook",
    "SelectionDecision",
    "SelectionMode",
    "SelectionOutcome",
    "SelectionReason",
    "SelectionStatus",
    "WarmupContext",
    "classify",
    "configuration_problem",
    "required_bars",
    "select",
]
