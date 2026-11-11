"""MarketDataService: the single downstream-facing entry point to market data.

Downstream code (API, future strategy / backtest / paper-trading engines) never talks to a provider or to
yfinance directly. It asks this service, which:
  * resolves instruments and refuses disabled ones (futures) before any provider call;
  * fetches through an explicitly chosen provider adapter (no silent fallback);
  * normalises, validates, de-duplicates and finalises bars (pipeline.py);
  * archives with full provenance and restatement tracking (archive.py);
  * records provider health and ingestion batches, including failures;
  * serves archived candles, coverage and freshness as provider-independent results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.enums import BatchStatus, InstrumentType, Timeframe, TradingStatus, VerificationStatus
from app.db.models import (
    AdjustedCandle,
    Candle,
    DataIngestionBatch,
    DataProvider,
    DataQualityIssue,
    DataRestatementEvent,
    ExchangeCalendarDay,
    Instrument,
    InstrumentProviderSymbol,
    ProviderCapability,
    ProviderHealthState,
)
from app.marketdata import archive
from app.marketdata.calendar import (
    HOLIDAY,
    SPECIAL_SESSION,
    TradingCalendar,
    SessionOverride,
    timeframe_minutes,
)
from app.marketdata.errors import InstrumentDisabled, NoDataReturned, ProviderError, UnsupportedCapability
from app.marketdata.freshness import Freshness, ProviderHealth, Thresholds, assess_freshness, assess_provider, worst
from app.marketdata.gaps import compute_coverage, probable_closures
from app.marketdata.pipeline import json_safe, process_bars
from app.marketdata.providers.registry import ProviderRegistry
from app.marketdata.registry import ALL_ENTRIES, EXCHANGE, EXCHANGE_TIMEZONE, CURRENCY
from app.marketdata.types import (
    Capability,
    CapabilityStatus,
    DataStatus,
    IssueType,
    QualityIssue,
    Severity,
)

log = logging.getLogger("agentx.marketdata")
THRESHOLD_STATUS = "PROVISIONAL"  # retained by the owner for Phase 2 as descriptive settings; reviewed again before trading use


def utcnow() -> datetime:
    return datetime.now(UTC)


class InstrumentNotFound(Exception):
    pass


@dataclass
class BatchOutcome:
    batch_id: int
    instrument_id: int
    symbol: str
    status: BatchStatus
    rows_received: int = 0
    candles_inserted: int = 0
    duplicates_identical: int = 0
    restatements: int = 0
    quarantined: int = 0
    non_final_dropped: int = 0
    corporate_actions_recorded: int = 0
    error_kind: str | None = None
    error_message: str | None = None


def load_calendar(session: Session, exchange: str = EXCHANGE) -> TradingCalendar:
    rows = session.scalars(select(ExchangeCalendarDay).where(ExchangeCalendarDay.exchange == exchange)).all()
    return TradingCalendar(
        SessionOverride(
            r.day,
            HOLIDAY if r.kind.value == "HOLIDAY" else SPECIAL_SESSION,
            r.open_time,
            r.close_time,
            r.source,
        )
        for r in rows
    )


def thresholds_from(settings: Settings) -> Thresholds:
    return Thresholds(
        finalization_margin=timedelta(seconds=settings.marketdata_finalization_margin_seconds),
        stale_after=timedelta(minutes=settings.marketdata_stale_after_minutes),
        provider_unavailable_after_failures=settings.marketdata_provider_unavailable_after_failures,
    )


class MarketDataService:
    def __init__(self, session: Session, registry: ProviderRegistry, settings: Settings, clock=utcnow):
        self.session = session
        self.registry = registry
        self.settings = settings
        self.clock = clock
        self.thresholds = thresholds_from(settings)
        self.calendar = load_calendar(session)

    # ---- registry / resolution -----------------------------------------------------------

    def provider_row(self, code: str) -> DataProvider:
        row = self.session.scalar(select(DataProvider).where(DataProvider.code == code))
        if row is None:
            raise UnsupportedCapability(f"provider {code!r} is not registered in the database; run the seed command", provider=code)
        return row

    def get_instrument(self, instrument_id: int) -> Instrument:
        inst = self.session.get(Instrument, instrument_id)
        if inst is None:
            raise InstrumentNotFound(instrument_id)
        return inst

    def list_instruments(self, *, instrument_type: InstrumentType | None = None, enabled: bool | None = None) -> list[Instrument]:
        q = select(Instrument).order_by(Instrument.instrument_type, Instrument.symbol)
        if instrument_type:
            q = q.where(Instrument.instrument_type == instrument_type)
        if enabled is not None:
            q = q.where((Instrument.trading_status == TradingStatus.ENABLED) == enabled)
        return list(self.session.scalars(q))

    def provider_symbol(self, instrument: Instrument, provider: DataProvider) -> str:
        """The provider's symbol for an instrument. Raises InstrumentDisabled for disabled instruments
        (futures) before any provider call, and when no explicit mapping exists."""
        if instrument.trading_status is not TradingStatus.ENABLED or not instrument.is_active:
            raise InstrumentDisabled(f"{instrument.symbol} {instrument.instrument_type.value} is disabled: {instrument.disabled_reason or 'inactive'}")
        mapping = self.session.scalar(
            select(InstrumentProviderSymbol).where(
                InstrumentProviderSymbol.instrument_id == instrument.id, InstrumentProviderSymbol.provider_id == provider.id
            )
        )
        if mapping is None:
            raise InstrumentDisabled(f"{instrument.symbol} has no explicit symbol mapping for provider {provider.code!r}")
        return mapping.provider_symbol

    # ---- provider health ---------------------------------------------------------------

    def _health_row(self, provider_id: int) -> ProviderHealthState:
        row = self.session.get(ProviderHealthState, provider_id)
        if row is None:
            row = ProviderHealthState(provider_id=provider_id, consecutive_failures=0)
            self.session.add(row)
            self.session.flush()
        return row

    def _record_attempt(self, provider_id: int, *, ok: bool, error: ProviderError | None = None, market_ts: datetime | None = None, ingested: bool = False) -> None:
        now, row = self.clock(), self._health_row(provider_id)
        row.last_attempt_at = now
        if ok:
            row.last_success_at, row.consecutive_failures = now, 0
            if market_ts and (row.last_observed_market_ts is None or market_ts > row.last_observed_market_ts):
                row.last_observed_market_ts = market_ts
            if ingested:
                row.last_successful_ingestion_at = now
        else:
            row.last_failure_at, row.consecutive_failures = now, row.consecutive_failures + 1
            row.last_error_kind = error.kind if error else "UNEXPECTED"
            row.last_error_message = (str(error) if error else "")[:1000]

    def provider_health(self, provider_code: str) -> ProviderHealth:
        provider = self.provider_row(provider_code)
        return assess_provider(self.session.get(ProviderHealthState, provider.id), self.thresholds)

    def capabilities(self, provider_code: str) -> list[dict[str, Any]]:
        """Declared capabilities merged with probe evidence. Anything never established is UNKNOWN."""
        provider = self.provider_row(provider_code)
        stored = {c.capability: c for c in self.session.scalars(select(ProviderCapability).where(ProviderCapability.provider_id == provider.id))}
        out = []
        for cap in Capability:
            row = stored.get(cap)
            out.append(
                {
                    "capability": cap.value,
                    "status": row.status.value if row else CapabilityStatus.UNKNOWN.value,
                    "note": row.note if row else None,
                    "evidence": row.evidence if row else {},
                    "probe_id": row.probe_id if row else None,
                    "determined_at": row.determined_at if row else None,
                }
            )
        return out

    # ---- ingestion -------------------------------------------------------------------------

    def ingest_window(
        self, *, job_id, instrument: Instrument, provider_code: str, timeframe: str, start: datetime, end: datetime,
        include_corporate_actions: bool = True, pre_issues: list[QualityIssue] | None = None,
    ) -> BatchOutcome:
        """One provider request for one instrument, fully accounted for. Never raises for provider failures:
        the outcome (and a persisted FAILED batch) carries them, so one instrument cannot break the others."""
        provider = self.provider_row(provider_code)
        adapter = self.registry.get(provider_code)
        tf = Timeframe(timeframe)
        if timeframe not in adapter.descriptor.max_lookback_by_interval:
            raise UnsupportedCapability(f"{provider_code} has no verified lookback limit for {timeframe}; refusing to ingest it", provider=provider_code)
        symbol = self.provider_symbol(instrument, provider)  # raises InstrumentDisabled before any batch exists

        batch = DataIngestionBatch(
            job_id=job_id, instrument_id=instrument.id, provider_id=provider.id, timeframe=tf, provider_symbol=symbol,
            request_start=start, request_end=end, status=BatchStatus.RUNNING,
        )
        self.session.add(batch)
        self.session.commit()  # the batch exists durably before anything references it
        batch_id = batch.id

        try:
            return self._ingest_into_batch(batch_id, instrument, provider, adapter, symbol, tf, start, end, include_corporate_actions, pre_issues or [])
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            self.session.rollback()
            log.exception("ingestion batch %s failed", batch_id)
            batch = self.session.get(DataIngestionBatch, batch_id)
            batch.status, batch.error_kind, batch.error_message = BatchStatus.FAILED, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:1000]
            batch.finished_at = self.clock()
            self.session.commit()
            return BatchOutcome(batch_id, instrument.id, instrument.symbol, BatchStatus.FAILED, error_kind="INTERNAL_ERROR", error_message=batch.error_message)

    def _ingest_into_batch(self, batch_id, instrument, provider, adapter, symbol, tf, start, end, include_ca, pre_issues) -> BatchOutcome:
        batch = self.session.get(DataIngestionBatch, batch_id)
        issues: list[QualityIssue] = list(pre_issues)
        try:
            result = adapter.fetch_history(symbol, tf.value, start, end)
        except NoDataReturned as exc:
            # The provider answered, with nothing: connectivity is fine, the window simply has no data.
            self._record_attempt(provider.id, ok=True)
            batch.status, batch.error_kind, batch.error_message = BatchStatus.NO_DATA, exc.kind, str(exc)[:1000]
            batch.finished_at = self.clock()
            self.session.commit()
            return BatchOutcome(batch_id, instrument.id, instrument.symbol, BatchStatus.NO_DATA, error_kind=exc.kind, error_message=str(exc)[:300])
        except ProviderError as exc:
            self._record_attempt(provider.id, ok=False, error=exc)
            batch.status, batch.error_kind, batch.error_message = BatchStatus.FAILED, exc.kind, str(exc)[:1000]
            batch.finished_at = self.clock()
            self.session.commit()
            return BatchOutcome(batch_id, instrument.id, instrument.symbol, BatchStatus.FAILED, error_kind=exc.kind, error_message=str(exc)[:300])

        discard_edge = adapter.descriptor.truncates_first_bar_when_request_starts_inside_session and self.starts_inside_session(result.request_start)
        processed = process_bars(
            result, instrument_id=instrument.id, timeframe=tf.value, calendar=self.calendar,
            finalization_margin=self.thresholds.finalization_margin, discard_leading_edge_bar=discard_edge,
        )
        issues.extend(processed.issues)
        archived = archive.archive_candles(self.session, processed.candles, provider_id=provider.id, batch_id=batch_id)
        issues.extend(archived.issues)

        ca_recorded = 0
        if include_ca:
            try:
                actions = adapter.fetch_corporate_actions(symbol)
                ca_recorded, ca_issues = archive.record_corporate_actions(
                    self.session, instrument_id=instrument.id, provider_id=provider.id, source=provider.code,
                    batch_id=batch_id, observed_at=result.observed_at, actions=actions,
                )
                issues.extend(ca_issues)
            except ProviderError as exc:
                issues.append(QualityIssue(IssueType.CORPORATE_ACTIONS_UNAVAILABLE, Severity.WARNING, None, f"corporate actions could not be fetched: {exc}", {"kind": exc.kind}))

        for i in issues:
            self.session.add(
                DataQualityIssue(
                    batch_id=batch_id, instrument_id=instrument.id, issue_type=i.issue_type, severity=i.severity,
                    bar_start_ts=i.bar_start_ts, message=i.message[:2000],
                    details=json_safe({k: v for k, v in i.details.items() if k != "raw"}), raw_payload=json_safe(i.details.get("raw")),
                )
            )
        batch.status = BatchStatus.SUCCEEDED
        batch.rows_received = processed.rows_received
        batch.candles_inserted = archived.inserted
        batch.duplicates_identical = archived.duplicates_identical + processed.duplicates_collapsed
        batch.restatements = archived.restatements
        batch.quarantined = processed.quarantined
        batch.non_final_dropped = processed.non_final_dropped
        batch.corporate_actions_recorded = ca_recorded
        batch.response_first_ts, batch.response_last_ts, batch.observed_at = processed.first_ts, processed.last_ts, result.observed_at
        batch.finished_at = self.clock()
        newest_end = max((c.bar_end_ts for c in processed.candles), default=None)
        self._record_attempt(provider.id, ok=True, market_ts=newest_end, ingested=bool(archived.inserted or archived.duplicates_identical))
        self.session.commit()
        return BatchOutcome(
            batch_id, instrument.id, instrument.symbol, BatchStatus.SUCCEEDED, processed.rows_received, archived.inserted,
            batch.duplicates_identical, archived.restatements, processed.quarantined, processed.non_final_dropped, ca_recorded,
        )

    def starts_inside_session(self, ts: datetime) -> bool:
        """True when `ts` lies strictly inside a known session (the case in which the provider truncates the
        first returned bar). A start at or before the session open is clean."""
        session = self.calendar.session(self.calendar.local_date(ts))
        return session is not None and session.open_ts < ts < session.close_ts

    # ---- reads -------------------------------------------------------------------------------

    def active_provider_id(self) -> int:
        """Reads serve only the configured active provider, so series from different providers never mix
        (the one-canonical-row-per-bar rule is per provider)."""
        return self.provider_row(self.settings.marketdata_provider).id

    def get_candles(
        self, *, instrument_id: int, timeframe: str, start: datetime, end: datetime, limit: int, after: datetime | None = None, series: str = "raw"
    ) -> tuple[list[dict[str, Any]], datetime | None]:
        """Archived canonical candles in [start, end), oldest first. Returns (rows, next_cursor)."""
        instrument = self.get_instrument(instrument_id)
        if instrument.trading_status is not TradingStatus.ENABLED:
            raise InstrumentDisabled(f"{instrument.symbol} {instrument.instrument_type.value} is disabled: {instrument.disabled_reason}")
        tf = Timeframe(timeframe)
        q = select(Candle).where(
            Candle.instrument_id == instrument_id, Candle.provider_id == self.active_provider_id(), Candle.timeframe == tf,
            Candle.is_canonical, Candle.bar_start_ts >= start, Candle.bar_start_ts < end,
        ).order_by(Candle.bar_start_ts).limit(limit + 1)
        if after is not None:
            q = q.where(Candle.bar_start_ts > after)
        rows = list(self.session.scalars(q))
        nxt = rows[limit - 1].bar_start_ts if len(rows) > limit else None
        rows = rows[:limit]
        provider_codes = {p.id: p.code for p in self.session.scalars(select(DataProvider))}
        adjusted: dict[int, AdjustedCandle] = {}
        if series == "adjusted" and rows:
            for a in self.session.scalars(
                select(AdjustedCandle).where(AdjustedCandle.candle_id.in_([r.id for r in rows])).order_by(AdjustedCandle.computed_at)
            ):
                adjusted[a.candle_id] = a  # newest computation wins
            rows = [r for r in rows if r.id in adjusted]
        out = []
        for r in rows:
            item = {
                "candle_id": r.id, "bar_start_ts": r.bar_start_ts, "bar_end_ts": r.bar_end_ts, "session_date": r.session_date,
                "timeframe": r.timeframe.value, "provider": provider_codes.get(r.provider_id), "provider_symbol": r.provider_symbol,
                "price_basis": r.price_basis.value, "observation_version": r.observation_version, "is_canonical": r.is_canonical,
                "observed_at": r.observed_at, "ingested_at": r.ingested_at, "ingestion_batch_id": r.ingestion_batch_id,
                "is_final": r.is_final,
            }
            if series == "adjusted":
                a = adjusted[r.id]
                item.update(
                    series="adjusted", open=a.adj_open, high=a.adj_high, low=a.adj_low, close=a.adj_close, volume=a.adj_volume,
                    adjustment_method=a.adjustment_method.value, adjustment_factor=a.adjustment_factor,
                    corporate_action_set_version=a.corporate_action_set_version,
                )
            else:
                item.update(series="raw", open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
            out.append(item)
        return out, nxt

    def coverage(self, *, instrument_id: int, timeframe: str, start_day: date, end_day: date, sample_limit: int = 100) -> dict[str, Any]:
        instrument = self.get_instrument(instrument_id)
        tf = Timeframe(timeframe)
        now = self.clock()
        provider_id = self.active_provider_id()
        sessions = self.calendar.sessions_between(start_day, end_day)
        present = set(
            self.session.scalars(
                select(Candle.bar_start_ts).where(
                    Candle.instrument_id == instrument_id, Candle.provider_id == provider_id, Candle.timeframe == tf,
                    Candle.is_canonical, Candle.session_date >= start_day, Candle.session_date <= end_day,
                )
            )
        )
        cov = compute_coverage(present, sessions, self.calendar, timeframe, until=now)
        evaluated_days = [d for d in cov.days if d.expected > 0]  # sessions that have begun
        closures = self._probable_closures(tf, start_day, end_day, now) if instrument.trading_status is TradingStatus.ENABLED else []
        absent = [d for d in cov.absent_days if d in {e.day for e in evaluated_days}]
        unexplained = [d for d in absent if d not in closures]
        first_last = self.session.execute(
            select(func.min(Candle.bar_start_ts), func.max(Candle.bar_end_ts), func.count()).where(
                Candle.instrument_id == instrument_id, Candle.provider_id == provider_id, Candle.timeframe == tf,
                Candle.is_canonical, Candle.session_date >= start_day, Candle.session_date <= end_day,
            )
        ).one()
        issue_counts = dict(
            self.session.execute(
                select(DataQualityIssue.issue_type, func.count()).where(DataQualityIssue.instrument_id == instrument_id).group_by(DataQualityIssue.issue_type)
            ).all()
        )
        restated = self.session.scalar(
            select(func.count()).select_from(DataRestatementEvent).where(
                DataRestatementEvent.instrument_id == instrument_id, DataRestatementEvent.provider_id == provider_id,
                DataRestatementEvent.timeframe == tf,
            )
        )
        missing_excl_absent = sum(len(d.missing_starts) for d in cov.days if d.present > 0)
        all_missing = [m for d in cov.days if d.present > 0 for m in d.missing_starts]
        first_expected = next(
            (t for s in sessions if s.day not in closures for t in self.calendar.expected_bar_starts(s, timeframe)), None
        )
        # "Full" must be earned: data present, no partial days, no unexplained absences, and the archive reaches
        # back to the first slot that should have traded. (Provider NaN gaps make this rare; that is the truth.)
        full = bool(present) and not cov.partial_days and not unexplained and first_expected is not None and (
            first_last[0] is not None and first_last[0] <= first_expected
        )
        return {
            "instrument_id": instrument_id, "symbol": instrument.symbol, "timeframe": timeframe,
            "requested_start_date": start_day, "requested_end_date": end_day,
            "first_candle_start": first_last[0], "last_candle_end": first_last[1], "candle_count": first_last[2],
            "expected_candles": cov.expected_total, "missing_intervals": cov.missing_total,
            "missing_intervals_within_present_days": missing_excl_absent,
            "session_days_evaluated": len(evaluated_days),
            "days_complete": len([d for d in evaluated_days if d.status == "COMPLETE"]),
            "days_partial": len([d for d in evaluated_days if d.status == "PARTIAL"]),
            "days_absent": len(absent),
            "partial_days": [{"day": d.day, "expected": d.expected, "present": d.present, "missing": len(d.missing_starts)} for d in cov.partial_days][:sample_limit],
            "absent_days": absent[:sample_limit],
            "probable_market_closures_inferred": [d for d in absent if d in closures],
            "unexplained_absences": unexplained[:sample_limit],
            "missing_interval_sample": all_missing[:sample_limit],
            "unexpected_archived_slots": cov.unexpected_present[:sample_limit],
            "quality_issue_counts": {k.value: v for k, v in issue_counts.items()},
            "restated_bars": restated,
            "full_coverage_of_requested_range": full,
            "calendar": {
                "source": self.calendar.source, "explicit_overrides_loaded": self.calendar.known_overrides,
                "authoritative_holiday_source": None, "inferred_closures_are_verified_holidays": False,
            },
            "notes": [
                "No authoritative NSE holiday source is approved or integrated (O13). Absent weekday sessions are reported "
                "separately; those absent for every enabled instrument are listed as inferred closures, which are not "
                "verified exchange holidays. Any other absence stays unexplained and is never classified as a holiday.",
                "NaN rows supplied by the provider are quarantined, not archived, so they show up as missing intervals.",
            ],
            "evaluated_until": now,
        }

    def _probable_closures(self, tf: Timeframe, start_day: date, end_day: date, now: datetime) -> list[date]:
        """Days with no archived bar for ANY enabled instrument although other days have data: inferred closures."""
        enabled = [i.id for i in self.list_instruments(enabled=True)]
        provider_id = self.active_provider_id()
        sessions = [s for s in self.calendar.sessions_between(start_day, end_day) if s.open_ts < now]
        by_inst: dict[str, list[date]] = {}
        for iid in enabled:
            days_with = set(
                self.session.scalars(
                    select(Candle.session_date).where(
                        Candle.instrument_id == iid, Candle.provider_id == provider_id, Candle.timeframe == tf,
                        Candle.is_canonical, Candle.session_date >= start_day, Candle.session_date <= end_day,
                    ).distinct()
                )
            )
            if days_with:
                by_inst[str(iid)] = [s.day for s in sessions if s.day not in days_with]
        return probable_closures(by_inst, [s.day for s in sessions])

    def freshness(self, *, instrument_id: int, timeframe: str) -> dict[str, Any]:
        instrument = self.get_instrument(instrument_id)
        tf = Timeframe(timeframe)
        latest_end = self.session.scalar(
            select(func.max(Candle.bar_end_ts)).where(
                Candle.instrument_id == instrument_id, Candle.provider_id == self.active_provider_id(), Candle.timeframe == tf,
                Candle.is_canonical,
            )
        )
        f: Freshness = assess_freshness(latest_bar_end=latest_end, now=self.clock(), calendar=self.calendar, timeframe=timeframe, thresholds=self.thresholds)
        return {
            "instrument_id": instrument_id, "symbol": instrument.symbol, "timeframe": timeframe,
            "status": f.status.value, "reason": f.reason,
            "latest_finalized_bar_end": f.latest_bar_end, "expected_latest_bar_end": f.expected_latest_bar_end,
            "data_age_seconds": f.data_age_seconds, "lag_seconds": f.lag_seconds, "lag_bars": f.lag_bars,
        }

    def thresholds_report(self) -> dict[str, Any]:
        t = self.thresholds
        note = (
            "Provisional Phase 2 setting (configurable). It describes data only: it is not guaranteed provider "
            "behaviour and not an approved trading-risk rule. Do not use to gate trading without a separate review."
        )
        return {
            "finalization_margin_seconds": {"value": t.finalization_margin.total_seconds(), "status": THRESHOLD_STATUS, "note": note},
            "stale_after_minutes": {"value": t.stale_after.total_seconds() / 60, "status": THRESHOLD_STATUS, "note": note},
            "provider_unavailable_after_consecutive_failures": {"value": t.provider_unavailable_after_failures, "status": THRESHOLD_STATUS, "note": note},
        }
