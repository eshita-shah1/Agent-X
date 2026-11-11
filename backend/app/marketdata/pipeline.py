"""Normalisation, validation, duplicate handling and finalisation of provider bars. Pure: no I/O.

Rules (each rejection is recorded as a QualityIssue; nothing is silently corrected or filled):
  * timestamps must be timezone-aware (a naive timestamp is rejected: no verified rule exists for assuming one);
  * timestamps are converted to UTC and must sit on the timeframe grid, inside a known session;
  * OHLC must be finite, positive and internally consistent; volume must be a non-negative whole number;
  * rows with all-NaN prices (Yahoo's marker for a missing bar) are quarantined, never archived;
  * identical duplicates collapse to one row; conflicting duplicates are quarantined entirely;
  * a bar is archived only once it is *final*: observed_at >= bar_end + finalisation margin.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar, timeframe_minutes
from app.marketdata.types import (
    IssueType,
    NormalizedCandle,
    ProviderBarsResult,
    QualityIssue,
    RawBar,
    Severity,
)

PRICE_QUANTUM = Decimal("0.000001")  # prices are stored to 6 decimal places (NUMERIC(18,6))
MAX_PRICE = Decimal("1e11")


@dataclass
class ProcessedBars:
    candles: list[NormalizedCandle] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    rows_received: int = 0
    quarantined: int = 0
    non_final_dropped: int = 0
    duplicates_collapsed: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None


def json_safe(obj):
    """Make a value storable as JSON/JSONB. PostgreSQL rejects NaN and Infinity, which are exactly what a
    provider's gap markers contain, so non-finite floats are kept as the strings 'nan' / 'inf' / '-inf'."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return repr(obj)
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def _raw_payload(raw: RawBar) -> dict:
    return json_safe(
        {
            "start_ts": raw.start_ts.isoformat() if raw.start_ts else None,
            "open": raw.open,
            "high": raw.high,
            "low": raw.low,
            "close": raw.close,
            "volume": raw.volume,
            "extras": raw.extras,
        }
    )


def _row_signature(raw: RawBar) -> str:
    return str((raw.open, raw.high, raw.low, raw.close, raw.volume))  # str() so NaN equals NaN


def _isnan(v: float | None) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def to_decimal(value: float) -> Decimal:
    """Float -> Decimal via repr (exact shortest float text), quantised to 6 dp with banker's rounding."""
    return Decimal(repr(float(value))).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def process_bars(
    result: ProviderBarsResult,
    *,
    instrument_id: int,
    timeframe: str,
    calendar: TradingCalendar,
    finalization_margin: timedelta,
    discard_leading_edge_bar: bool = False,
) -> ProcessedBars:
    """`discard_leading_edge_bar`: the provider truncates the first bar when the request starts inside a
    session (measured for Yahoo). That bar is quarantined instead of archived."""
    out = ProcessedBars(rows_received=len(result.bars))
    minutes = timeframe_minutes(timeframe)
    step = timedelta(minutes=minutes)
    observed_at = result.observed_at

    def reject(issue_type: IssueType, severity: Severity, start: datetime | None, message: str, raw: RawBar | None, **details):
        d = json_safe(dict(details))
        if raw is not None:
            d["raw"] = _raw_payload(raw)
        out.issues.append(QualityIssue(issue_type, severity, start, message, d))
        out.quarantined += 1

    # 1. timestamps
    grouped: dict[datetime, list[RawBar]] = defaultdict(list)
    for raw in result.bars:
        ts = raw.start_ts
        if ts is None:
            reject(IssueType.INVALID_TIMESTAMP, Severity.ERROR, None, "row has no usable timestamp", raw)
            continue
        if ts.tzinfo is None:
            reject(IssueType.NAIVE_TIMESTAMP, Severity.ERROR, None, "timestamp is not timezone-aware; no verified rule exists to interpret it", raw)
            continue
        start = ts.astimezone(UTC)
        local = start.astimezone(EXCHANGE_TZ)
        if local.minute % minutes or local.second or local.microsecond:
            reject(IssueType.MISALIGNED_TIMESTAMP, Severity.ERROR, start, f"timestamp {local:%H:%M:%S} is not on the {timeframe} grid", raw)
            continue
        grouped[start].append(raw)

    if grouped:
        out.first_ts, out.last_ts = min(grouped), max(grouped)

    # The provider truncates the first bar when the request starts inside a session. Only a bar that begins
    # within one step of the request start can be that bar (later rows are untouched).
    edge_bar = None
    if discard_leading_edge_bar and grouped:
        first = min(grouped)
        if first < result.request_start.astimezone(UTC) + step:
            edge_bar = first

    # 2. per-timestamp: duplicates, values, session, finality
    for start in sorted(grouped):
        rows = grouped[start]
        if start == edge_bar:
            for r in rows:
                reject(
                    IssueType.WINDOW_EDGE_TRUNCATED, Severity.WARNING, start,
                    "first bar of a request that started inside a session is truncated by the provider; not archived", r,
                    request_start=result.request_start.isoformat(),
                )
            continue
        if len(rows) > 1:
            if len({_row_signature(r) for r in rows}) == 1:
                out.duplicates_collapsed += len(rows) - 1
                out.issues.append(QualityIssue(IssueType.DUPLICATE_IDENTICAL, Severity.INFO, start, f"{len(rows)} identical rows collapsed to one", {"count": len(rows)}))
            else:
                for r in rows:
                    reject(IssueType.DUPLICATE_CONFLICTING, Severity.ERROR, start, "conflicting rows share a timestamp; all quarantined", r)
                continue
        raw = rows[0]
        end = start + step

        prices = [raw.open, raw.high, raw.low, raw.close]
        if all(_isnan(p) for p in prices):
            reject(
                IssueType.NON_NUMERIC_VALUE, Severity.WARNING, start,
                "all prices are missing (provider gap marker); not archived", raw,
                provider_gap_marker=True, volume=raw.volume,
            )
            continue
        if any(_isnan(p) for p in prices):
            reject(IssueType.NON_NUMERIC_VALUE, Severity.ERROR, start, "some prices are missing", raw)
            continue
        if any(math.isinf(p) for p in prices):
            reject(IssueType.NON_FINITE_VALUE, Severity.ERROR, start, "infinite price", raw)
            continue
        try:
            o, h, l, c = (to_decimal(p) for p in prices)
        except (InvalidOperation, ValueError, TypeError):
            reject(IssueType.NON_NUMERIC_VALUE, Severity.ERROR, start, "price is not numeric", raw)
            continue
        if min(o, h, l, c) <= 0 or max(o, h, l, c) >= MAX_PRICE:
            reject(IssueType.NON_POSITIVE_PRICE, Severity.ERROR, start, "price is not within (0, 1e11)", raw)
            continue
        if not (h >= l and h >= o and h >= c and l <= o and l <= c):
            reject(IssueType.INVALID_OHLC, Severity.ERROR, start, "OHLC relationships violated", raw)
            continue
        vol = raw.volume
        if _isnan(vol) or math.isinf(vol) or vol < 0 or vol != int(vol):
            reject(IssueType.INVALID_VOLUME, Severity.ERROR, start, "volume is missing, negative or not a whole number", raw)
            continue
        if not calendar.bar_is_in_session(start, timeframe):
            reject(
                IssueType.OUT_OF_SESSION, Severity.WARNING, start,
                f"bar {start.astimezone(EXCHANGE_TZ):%Y-%m-%d %H:%M} IST is outside any known session "
                "(a holiday or special session not in the calendar looks the same)", raw,
            )
            continue
        beyond_request = end > result.request_end.astimezone(UTC)  # a bar cut off by the request's own end is incomplete
        if observed_at < end + finalization_margin or beyond_request:
            out.non_final_dropped += 1
            out.issues.append(
                QualityIssue(
                    IssueType.INCOMPLETE_CANDLE, Severity.INFO, start,
                    "bar not yet final; not archived",
                    {"bar_end": end.isoformat(), "margin_seconds": finalization_margin.total_seconds(), "ends_after_request_end": beyond_request},
                )
            )
            continue

        out.candles.append(
            NormalizedCandle(
                instrument_id=instrument_id,
                provider_code=result.provider_code,
                provider_symbol=result.provider_symbol,
                timeframe=timeframe,
                bar_start_ts=start,
                bar_end_ts=end,
                session_date=start.astimezone(EXCHANGE_TZ).date(),
                open=o, high=h, low=l, close=c,
                volume=int(vol),
                observed_at=observed_at,
                source_metadata={
                    "library": result.meta.get("library"),
                    "library_version": result.meta.get("library_version"),
                    "request_params": result.meta.get("params"),
                    "price_quantization": "6dp half-even from provider float",
                },
            )
        )
    return out
