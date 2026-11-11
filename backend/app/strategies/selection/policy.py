"""Selection policies: regime-gated priority (mode A) and a pinned configuration (mode B).

Structural mistakes (no candidates, duplicate ids or priorities) are rejected when the policy is built. A candidate
whose strategy or configuration is unusable is accepted here and reported in every decision instead: it is never
silently replaced (pinned mode gives NO_SELECTION rather than falling back).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.strategies.selection.candidates import Candidate
from app.strategies.selection.regime import RegimeConfig


class SelectionMode(str, enum.Enum):
    REGIME_PRIORITY = "REGIME_PRIORITY"
    PINNED = "PINNED"


@dataclass(frozen=True)
class RegimePriorityPolicy:
    candidates: tuple[Candidate, ...]
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    mode = SelectionMode.REGIME_PRIORITY

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise ValueError("candidates must be a non-empty tuple")
        if not all(isinstance(c, Candidate) for c in self.candidates):
            raise ValueError("candidates must be Candidate objects")
        if not isinstance(self.regime, RegimeConfig):
            raise ValueError("regime must be a RegimeConfig")
        if any(c.priority is None for c in self.candidates):
            raise ValueError("every candidate needs an explicit priority in regime-priority mode")
        ids, priorities = [c.config_id for c in self.candidates], [c.priority for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("config_id values must be unique")
        if len(set(priorities)) != len(priorities):
            raise ValueError("priorities must be unique (no ties to break)")

    def ordered(self) -> tuple[Candidate, ...]:
        return tuple(sorted(self.candidates, key=lambda c: c.priority))


@dataclass(frozen=True)
class PinnedPolicy:
    candidate: Candidate
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    mode = SelectionMode.PINNED

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise ValueError("candidate must be a Candidate")
        if not isinstance(self.regime, RegimeConfig):
            raise ValueError("regime must be a RegimeConfig")


Policy = RegimePriorityPolicy | PinnedPolicy
