"""Persisting validated candles and corporate actions.

Guarantees:
  * re-ingesting an unchanged candle is a no-op (idempotent);
  * a candle whose values differ from what is already archived is stored as an additional, NON-canonical
    version and recorded as a restatement event - the archived observation is never touched;
  * nothing is deleted; old archived candles stay even when they fall outside the provider's lookback window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import CorporateActionType, Timeframe
from app.db.models import Candle, CorporateAction, DataRestatementEvent
from app.marketdata.types import IssueType, NormalizedCandle, QualityIssue, RawCorporateAction, Severity

VALUE_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class ArchiveResult:
    inserted: int = 0
    duplicates_identical: int = 0
    restatements: int = 0
    issues: list[QualityIssue] = field(default_factory=list)


def _values(obj) -> tuple:
    return tuple(getattr(obj, f) for f in VALUE_FIELDS)


def archive_candles(
    session: Session, candles: list[NormalizedCandle], *, provider_id: int, batch_id: int
) -> ArchiveResult:
    result = ArchiveResult()
    if not candles:
        return result
    instrument_id, timeframe = candles[0].instrument_id, Timeframe(candles[0].timeframe)
    lo, hi = min(c.bar_start_ts for c in candles), max(c.bar_start_ts for c in candles)

    existing: dict[datetime, list[Candle]] = {}
    for row in session.scalars(
        select(Candle).where(
            Candle.instrument_id == instrument_id,
            Candle.provider_id == provider_id,
            Candle.timeframe == timeframe,
            Candle.bar_start_ts.between(lo, hi),
        )
    ):
        existing.setdefault(row.bar_start_ts, []).append(row)

    new_rows: list[tuple[Candle, Candle | None, dict | None]] = []  # (row, canonical-it-restates, changed fields)
    for c in candles:
        versions = existing.get(c.bar_start_ts, [])
        incoming = (c.open, c.high, c.low, c.close, c.volume)
        if any(_values(v) == incoming for v in versions):
            result.duplicates_identical += 1  # already archived, unchanged (possibly as an earlier restatement)
            continue
        canonical = next((v for v in versions if v.is_canonical), None)
        row = Candle(
            instrument_id=c.instrument_id,
            provider_id=provider_id,
            timeframe=timeframe,
            bar_start_ts=c.bar_start_ts,
            bar_end_ts=c.bar_end_ts,
            session_date=c.session_date,
            open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            is_final=True,
            observed_at=c.observed_at,
            provider_symbol=c.provider_symbol,
            ingestion_batch_id=batch_id,
            source_metadata=c.source_metadata,
            observation_version=(max(v.observation_version for v in versions) + 1) if versions else 1,
            is_canonical=canonical is None,  # first observation is canonical; restatements are kept alongside
        )
        changed = None
        if canonical is not None:
            changed = {f: [str(getattr(canonical, f)), str(getattr(c, f))] for f in VALUE_FIELDS if getattr(canonical, f) != getattr(c, f)}
        session.add(row)
        new_rows.append((row, canonical, changed))
    session.flush()

    for row, canonical, changed in new_rows:
        if canonical is None:
            result.inserted += 1
            continue
        result.restatements += 1
        session.add(
            DataRestatementEvent(
                instrument_id=row.instrument_id,
                provider_id=provider_id,
                timeframe=timeframe,
                bar_start_ts=row.bar_start_ts,
                original_candle_id=canonical.id,
                restated_candle_id=row.id,
                batch_id=batch_id,
                changed_fields=changed,
            )
        )
        result.issues.append(
            QualityIssue(
                IssueType.RESTATEMENT, Severity.WARNING, row.bar_start_ts,
                "provider reported different values for an archived bar; both observations kept",
                {"canonical_candle_id": canonical.id, "restated_candle_id": row.id, "changed_fields": changed},
            )
        )
    session.flush()
    return result


def record_corporate_actions(
    session: Session,
    *,
    instrument_id: int,
    provider_id: int,
    source: str,
    batch_id: int,
    observed_at: datetime,
    actions: list[RawCorporateAction],
) -> tuple[int, list[QualityIssue]]:
    """Insert unseen actions; never overwrite. A provider revising an already-recorded action is reported."""
    inserted, issues = 0, []
    existing = {
        (a.action_type, a.ex_date): a
        for a in session.scalars(select(CorporateAction).where(CorporateAction.instrument_id == instrument_id, CorporateAction.source == source))
    }
    for a in actions:
        kind = CorporateActionType.DIVIDEND if a.kind == "DIVIDEND" else CorporateActionType.SPLIT
        value = Decimal(repr(a.value)).quantize(Decimal("0.000001"))
        have = existing.get((kind, a.ex_date))
        if have is not None:
            recorded = have.cash_amount if kind is CorporateActionType.DIVIDEND else have.ratio_numerator
            if recorded != value:
                issues.append(
                    QualityIssue(
                        IssueType.RESTATEMENT, Severity.WARNING, None,
                        f"{kind.value} on {a.ex_date} reported with a different value than archived; archived value kept",
                        {"archived": str(recorded), "reported": str(value), "ex_date": a.ex_date.isoformat(), "kind": "corporate_action"},
                    )
                )
            continue
        session.add(
            CorporateAction(
                instrument_id=instrument_id,
                action_type=kind,
                ex_date=a.ex_date,
                ratio_numerator=None if kind is CorporateActionType.DIVIDEND else value,
                ratio_denominator=None if kind is CorporateActionType.DIVIDEND else Decimal(1),
                cash_amount=value if kind is CorporateActionType.DIVIDEND else None,
                source=source,
                provider_id=provider_id,
                ingestion_batch_id=batch_id,
                observed_at=observed_at,
                provider_raw={"kind": a.kind, "value": a.value},
                notes="provider does not distinguish a bonus issue from a split" if kind is CorporateActionType.SPLIT else None,
            )
        )
        inserted += 1
    session.flush()
    return inserted, issues


def latest_canonical_bar_end(session: Session, instrument_id: int, provider_id: int, timeframe: Timeframe) -> datetime | None:
    return session.scalar(
        select(func.max(Candle.bar_end_ts)).where(
            Candle.instrument_id == instrument_id,
            Candle.provider_id == provider_id,
            Candle.timeframe == timeframe,
            Candle.is_canonical,
        )
    )
