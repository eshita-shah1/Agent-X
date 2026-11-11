"""Opt-in Yahoo Finance capability and coverage probe.

NOT part of the automated test suite (it needs internet and Yahoo's goodwill). Run explicitly:

    python -m app.marketdata.probe [--symbols RELIANCE,TCS] [--days 60] [--out DIR]

It records what Yahoo actually returned. It never fabricates, repairs or fills data: anything it cannot
establish is reported as unknown or inconclusive.

The `analyze_*` functions are pure so they can be unit-tested against fixtures.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar, timeframe_minutes
from app.marketdata.errors import ProviderError, RateLimited
from app.marketdata.gaps import compute_coverage
from app.marketdata.providers.yahoo import HISTORY_PARAMS, YahooFinanceProvider
from app.marketdata.registry import APPROVED_EQUITIES
from app.marketdata.types import RawBar

PRICE_COLUMNS = ("open", "high", "low", "close")


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    return _iso(obj)


# ---- pure analysis ----------------------------------------------------------------------------


def analyze_intraday(
    bars: list[RawBar],
    *,
    interval: str,
    observed_at: datetime,
    request_start: datetime,
    request_end: datetime,
    calendar: TradingCalendar | None = None,
) -> dict[str, Any]:
    calendar = calendar or TradingCalendar()
    minutes = timeframe_minutes(interval)
    out: dict[str, Any] = {"interval": interval, "returned_candles": len(bars)}
    aware = [b for b in bars if b.start_ts is not None and b.start_ts.tzinfo is not None]
    out["naive_or_missing_timestamps"] = len(bars) - len(aware)
    if not aware:
        out["note"] = "no timezone-aware timestamps returned"
        return out

    starts = [b.start_ts.astimezone(UTC) for b in aware]
    out["first_bar_start_utc"] = min(starts)
    out["last_bar_start_utc"] = max(starts)
    out["first_bar_start_ist"] = min(starts).astimezone(EXCHANGE_TZ)
    out["last_bar_start_ist"] = max(starts).astimezone(EXCHANGE_TZ)
    last_end = max(starts) + timedelta(minutes=minutes)
    out["last_bar_end_utc"] = last_end
    out["last_bar_lag_minutes"] = round((observed_at - last_end).total_seconds() / 60, 1)

    # row quality
    nan_rows = nonfinite = ohlc_bad = zero_vol = neg_vol = missing_vol = 0
    for b in aware:
        vals = [getattr(b, c) for c in PRICE_COLUMNS]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals):
            nan_rows += 1
            continue
        if any(math.isinf(v) for v in vals):
            nonfinite += 1
            continue
        if not (b.high >= b.low and b.high >= b.open and b.high >= b.close and b.low <= b.open and b.low <= b.close):
            ohlc_bad += 1
        if b.volume is None or math.isnan(b.volume):
            missing_vol += 1
        elif b.volume < 0:
            neg_vol += 1
        elif b.volume == 0:
            zero_vol += 1
    out.update(
        nan_price_rows=nan_rows,
        non_finite_rows=nonfinite,
        ohlc_relationship_violations=ohlc_bad,
        zero_volume_bars=zero_vol,
        negative_volume_bars=neg_vol,
        missing_volume_bars=missing_vol,
    )

    # duplicates and alignment
    seen: dict[datetime, list[RawBar]] = defaultdict(list)
    for b, s in zip(aware, starts):
        seen[s].append(b)
    dup_ts = {s: v for s, v in seen.items() if len(v) > 1}
    conflicting = sum(
        1 for v in dup_ts.values() if len({(x.open, x.high, x.low, x.close, x.volume) for x in v}) > 1
    )
    out["duplicate_timestamps"] = len(dup_ts)
    out["conflicting_duplicate_timestamps"] = conflicting
    misaligned = [s for s in set(starts) if s.astimezone(EXCHANGE_TZ).minute % minutes or s.second or s.microsecond]
    out["misaligned_timestamps"] = len(misaligned)
    out["out_of_session_bars"] = sorted(s for s in set(starts) if not calendar.bar_is_in_session(s, interval))[:10]
    out["out_of_session_bar_count"] = sum(1 for s in set(starts) if not calendar.bar_is_in_session(s, interval))
    local_times = sorted({s.astimezone(EXCHANGE_TZ).strftime("%H:%M") for s in set(starts)})
    out["bar_start_time_of_day_min_max_ist"] = [local_times[0], local_times[-1]] if local_times else None

    # coverage against the calendar (weekday regular sessions; no holiday source)
    first_day = min(starts).astimezone(EXCHANGE_TZ).date()
    last_day = max(starts).astimezone(EXCHANGE_TZ).date()
    sessions = calendar.sessions_between(first_day, last_day)

    def _valid(b: RawBar) -> bool:
        vals = [getattr(b, c) for c in PRICE_COLUMNS]
        return all(v is not None and math.isfinite(v) for v in vals)

    # only rows with usable prices count as present: NaN rows are provider gaps, not data
    valid_starts = {s for b, s in zip(aware, starts) if _valid(b)}
    cov = compute_coverage(valid_starts, sessions, calendar, interval, until=observed_at)
    nan_times = defaultdict(int)
    for b, s in zip(aware, starts):
        if not _valid(b):
            nan_times[s.astimezone(EXCHANGE_TZ).strftime("%H:%M")] += 1
    out["invalid_rows_by_time_of_day_ist"] = dict(sorted(nan_times.items(), key=lambda kv: -kv[1])[:8])
    # the final (possibly unfinished) day is judged only up to the last returned bar
    out["calendar_assumption"] = calendar.source
    out["session_days_in_returned_span"] = len(sessions)
    out["days_complete"] = len(cov.complete_days)
    out["absent_weekday_sessions"] = cov.absent_days
    out["partial_days"] = {
        d.day.isoformat(): {"expected": d.expected, "present": d.present, "missing": len(d.missing_starts)}
        for d in cov.partial_days
    }
    out["missing_interval_total_excl_absent_days"] = sum(len(d.missing_starts) for d in cov.partial_days)
    out["sample_missing_intervals_ist"] = [
        m.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M") for d in cov.partial_days for m in d.missing_starts
    ][:15]
    # requested window vs. what came back
    out["requested_start"] = request_start
    out["requested_end"] = request_end
    out["actual_start_vs_requested_start_days"] = round(
        (min(starts) - request_start).total_seconds() / 86400, 2
    )

    # price formatting observations (float noise)
    off_paise = []
    for b in aware:
        for c in PRICE_COLUMNS:
            v = getattr(b, c)
            if v is not None and math.isfinite(v) and abs(v - round(v, 2)) > 1e-9:
                off_paise.append(abs(v - round(v, 2)))
    out["price_values_not_on_2dp"] = len(off_paise)
    out["max_deviation_from_2dp"] = max(off_paise) if off_paise else 0.0

    extras_keys: set[str] = set()
    for b in aware:
        extras_keys.update(b.extras.keys())
    out["extras_present"] = sorted(extras_keys)
    out["intraday_adj_close_differs_from_close"] = sum(
        1
        for b in aware
        if b.extras.get("adj_close") is not None and b.close is not None and abs(b.extras["adj_close"] - b.close) > 1e-9
    )
    out["intraday_rows_with_dividends_or_splits"] = sum(
        1 for b in aware if "dividends" in b.extras or "stock_splits" in b.extras
    )
    return out


def compare_overlap(first: list[RawBar], second: list[RawBar]) -> dict[str, Any]:
    """Compare two fetches of the same window (detects partial bars and immediate restatements)."""

    def key(bars):
        return {b.start_ts.astimezone(UTC): (b.open, b.high, b.low, b.close, b.volume) for b in bars if b.start_ts and b.start_ts.tzinfo}

    a, b = key(first), key(second)
    common = sorted(set(a) & set(b))
    changed = [t for t in common if str(a[t]) != str(b[t])]  # str() so NaN compares equal to NaN
    return {
        "compared_bars": len(common),
        "changed_bars": len(changed),
        "changed_bar_starts": changed[:5],
        "changed_bar_values": [{"start": t, "first": a[t], "second": b[t]} for t in changed[:3]],
        "only_in_first": len(set(a) - set(b)),
        "only_in_second": len(set(b) - set(a)),
        "last_bar_changed": bool(common) and common[-1] in changed,
    }


def _daily_by_date(bars: list[RawBar]) -> dict[date, RawBar]:
    out: dict[date, RawBar] = {}
    for b in bars:
        if b.start_ts is None:
            continue
        d = b.start_ts.astimezone(EXCHANGE_TZ).date() if b.start_ts.tzinfo else b.start_ts.date()
        out[d] = b
    return out


def analyze_price_basis(daily: list[RawBar], splits: list[tuple[date, float]]) -> dict[str, Any]:
    """Is the unadjusted `Close` genuinely as-traded? Tested at split ex-dates (a split must show as a price
    drop in as-traded data; if the series is continuous across it, the 'raw' Close is split-adjusted)."""
    by_date = _daily_by_date(daily)
    days = sorted(by_date)
    result: dict[str, Any] = {"daily_bars": len(days), "split_tests": []}

    adj_diff = [
        abs(b.extras["adj_close"] - b.close) / b.close
        for b in by_date.values()
        if b.extras.get("adj_close") is not None and b.close
    ]
    result["adj_close_present_rows"] = len(adj_diff)
    result["adj_close_differs_rows"] = sum(1 for x in adj_diff if x > 1e-6)
    result["max_relative_adj_close_difference"] = max(adj_diff) if adj_diff else None

    for ex_date, ratio in splits:
        if ex_date not in by_date:
            result["split_tests"].append({"ex_date": ex_date, "ratio": ratio, "verdict": "INCONCLUSIVE", "reason": "no daily bar on ex-date"})
            continue
        prev = [d for d in days if d < ex_date]
        if not prev:
            result["split_tests"].append({"ex_date": ex_date, "ratio": ratio, "verdict": "INCONCLUSIVE", "reason": "no prior daily bar in range"})
            continue
        prev_close, ex_close = by_date[prev[-1]].close, by_date[ex_date].close
        if not prev_close or not ex_close or ratio <= 0 or abs(math.log(ratio)) < math.log(1.5):
            result["split_tests"].append({"ex_date": ex_date, "ratio": ratio, "verdict": "INCONCLUSIVE", "reason": "ratio too close to 1 or missing close"})
            continue
        observed = prev_close / ex_close
        score = math.log(observed) / math.log(ratio)  # ~1 => as-traded drop; ~0 => continuous (adjusted)
        verdict = "AS_TRADED_LIKE" if score > 0.7 else "SPLIT_ADJUSTED_LIKE" if score < 0.3 else "INCONCLUSIVE"
        result["split_tests"].append(
            {"ex_date": ex_date, "split_ratio": ratio, "prev_close": prev_close, "ex_close": ex_close,
             "observed_prev_over_ex": round(observed, 4), "score": round(score, 3), "verdict": verdict}
        )
    verdicts = {t["verdict"] for t in result["split_tests"]}
    if "SPLIT_ADJUSTED_LIKE" in verdicts and "AS_TRADED_LIKE" not in verdicts:
        result["classification"] = "CLOSE_APPEARS_SPLIT_ADJUSTED"
    elif "AS_TRADED_LIKE" in verdicts and "SPLIT_ADJUSTED_LIKE" not in verdicts:
        result["classification"] = "CLOSE_APPEARS_AS_TRADED_AT_SPLITS"
    elif not result["split_tests"]:
        result["classification"] = "INCONCLUSIVE_NO_SPLITS_IN_RANGE"
    else:
        result["classification"] = "INCONCLUSIVE"
    result["label_applied"] = "UNVERIFIED_PROVIDER_NATIVE (no verification claimed)"
    return result


def analyze_daily_vs_intraday(daily: list[RawBar], intraday: list[RawBar], interval: str, calendar: TradingCalendar | None = None) -> dict[str, Any]:
    """Consistency between daily bars and aggregated intraday bars on complete sessions."""
    calendar = calendar or TradingCalendar()
    by_day: dict[date, list[RawBar]] = defaultdict(list)
    for b in intraday:
        if b.start_ts and b.start_ts.tzinfo:
            by_day[b.start_ts.astimezone(EXCHANGE_TZ).date()].append(b)
    daily_map = _daily_by_date(daily)
    rows = []
    for day, bars in sorted(by_day.items()):
        session = calendar.session(day)
        if session is None or day not in daily_map:
            continue
        expected = len(calendar.expected_bar_starts(session, interval))
        if len(bars) != expected:
            continue  # only compare complete sessions
        bars = sorted(bars, key=lambda b: b.start_ts)
        valid = [b for b in bars if all(v is not None and math.isfinite(v) for v in (b.open, b.high, b.low, b.close))]
        if not valid:
            continue
        d = daily_map[day]
        rows.append(
            {
                "day": day,
                "had_invalid_rows": len(valid) != len(bars),
                "open_diff_pct": abs(valid[0].open - d.open) / d.open * 100,
                "close_diff_pct": abs(valid[-1].close - d.close) / d.close * 100,
                "high_diff_pct": abs(max(b.high for b in valid) - d.high) / d.high * 100,
                "low_diff_pct": abs(min(b.low for b in valid) - d.low) / d.low * 100,
                "volume_diff_pct": abs(sum(b.volume or 0 for b in valid) - (d.volume or 0)) / d.volume * 100 if d.volume else None,
            }
        )
    if not rows:
        return {"complete_sessions_compared": 0}
    def mx(k):
        vals = [r[k] for r in rows if r[k] is not None]
        return round(max(vals), 4) if vals else None
    return {
        "complete_sessions_compared": len(rows),
        "sessions_with_invalid_rows": sum(1 for r in rows if r["had_invalid_rows"]),
        "note": "5m rows with NaN prices are excluded; 'close' compares the last valid 5m close with the daily close",
        "max_open_diff_pct": mx("open_diff_pct"),
        "max_close_diff_pct": mx("close_diff_pct"),
        "max_high_diff_pct": mx("high_diff_pct"),
        "max_low_diff_pct": mx("low_diff_pct"),
        "max_volume_diff_pct": mx("volume_diff_pct"),
    }


# ---- live probe ---------------------------------------------------------------------------------


def _step(fn, *, retries_on_rate_limit: int = 1, backoff: float = 10.0) -> dict[str, Any]:
    """Run one provider call; capture success/failure and timing. Never raises."""
    attempts = 0
    while True:
        started = time.monotonic()
        try:
            value = fn()
            return {"ok": True, "value": value, "elapsed_s": round(time.monotonic() - started, 2), "attempts": attempts + 1}
        except RateLimited as exc:
            attempts += 1
            if attempts > retries_on_rate_limit:
                return {"ok": False, "error_kind": exc.kind, "error": str(exc), "elapsed_s": round(time.monotonic() - started, 2), "attempts": attempts}
            time.sleep(backoff)
        except ProviderError as exc:
            return {"ok": False, "error_kind": exc.kind, "error": str(exc), "elapsed_s": round(time.monotonic() - started, 2), "attempts": attempts + 1}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error_kind": "UNEXPECTED", "error": f"{type(exc).__name__}: {exc}", "elapsed_s": round(time.monotonic() - started, 2), "attempts": attempts + 1}


def probe_symbol(provider: YahooFinanceProvider, symbol: str, provider_symbol: str, *, days: int, now: datetime) -> dict[str, Any]:
    report: dict[str, Any] = {"symbol": symbol, "provider_symbol": provider_symbol, "probed_at": datetime.now(UTC)}
    calendar = TradingCalendar()

    # 1. five-minute history: find the longest accepted lookback at or below the target
    # Exactly `days` days is rejected by Yahoo only because time elapses before the server evaluates the
    # request; a start slightly inside the window is accepted. Try that first, then shorter fallbacks.
    attempts, first_ok = [], None
    safety = timedelta(minutes=10)
    for lookback in (days, days - 1, days - 2):
        offset = timedelta(days=lookback) - (safety if lookback == days else timedelta(0))
        start, end = now - offset, now
        res = _step(lambda s=start, e=end: provider.fetch_history(provider_symbol, "5m", s, e))
        attempts.append({"lookback_days": lookback, "start_inside_limit_by_minutes": 10 if lookback == days else 0, "ok": res["ok"], "error_kind": res.get("error_kind"), "error": res.get("error"), "elapsed_s": res["elapsed_s"]})
        if res["ok"]:
            first_ok = (lookback, start, end, res["value"])
            break
        if res.get("error_kind") in ("RATE_LIMITED", "UNAVAILABLE"):
            break  # not a range problem; shorter ranges will not help
    report["five_minute_attempts"] = attempts
    report["requested_days"] = days
    if first_ok is None:
        report["five_minute"] = {"success": False}
        report["success"] = False
        return report
    lookback, start, end, result = first_ok
    report["accepted_lookback_days"] = lookback
    report["reported_timezone"] = result.reported_timezone
    report["request_meta"] = result.meta
    report["five_minute"] = {"success": True, **analyze_intraday(result.bars, interval="5m", observed_at=result.observed_at, request_start=start, request_end=end, calendar=calendar)}
    report["fetch_observed_at"] = result.observed_at

    # 2. immediate refetch of the last 3 days: partial-bar / restatement observability
    r2 = _step(lambda: provider.fetch_history(provider_symbol, "5m", now - timedelta(days=3), datetime.now(UTC)))
    if r2["ok"]:
        report["immediate_refetch_comparison"] = compare_overlap(result.bars, r2["value"].bars)
    else:
        report["immediate_refetch_comparison"] = {"success": False, "error_kind": r2.get("error_kind"), "error": r2.get("error")}

    # 3. daily history (5y) for price-basis and consistency tests
    d_start = now - timedelta(days=5 * 365)
    daily = _step(lambda: provider.fetch_history(provider_symbol, "1d", d_start, now))
    actions = _step(lambda: provider.fetch_corporate_actions(provider_symbol))
    splits: list[tuple[date, float]] = []
    if actions["ok"]:
        acts = actions["value"]
        splits = [(a.ex_date, a.value) for a in acts if a.kind == "SPLIT"]
        divs = [a for a in acts if a.kind == "DIVIDEND"]
        report["corporate_actions"] = {
            "success": True,
            "dividends": len(divs),
            "splits": len(splits),
            "split_events": [{"ex_date": d, "ratio_new_per_old": r} for d, r in splits],
            "first_event": min((a.ex_date for a in acts), default=None),
            "last_event": max((a.ex_date for a in acts), default=None),
        }
    else:
        report["corporate_actions"] = {"success": False, "error_kind": actions.get("error_kind"), "error": actions.get("error")}
    if daily["ok"]:
        dres = daily["value"]
        report["daily"] = {"success": True, "bars": len(dres.bars), "columns": dres.meta.get("columns")}
        report["price_basis"] = analyze_price_basis(dres.bars, splits)
        report["daily_vs_5m_consistency"] = analyze_daily_vs_intraday(dres.bars, result.bars, "5m", calendar)
    else:
        report["daily"] = {"success": False, "error_kind": daily.get("error_kind"), "error": daily.get("error")}

    # 4. quote and metadata
    q = _step(lambda: provider.fetch_quote(provider_symbol))
    report["quote"] = {"success": q["ok"], **({"last_price": str(q["value"].last_price), "currency": q["value"].currency, "market_ts": q["value"].market_ts} if q["ok"] else {"error_kind": q.get("error_kind"), "error": q.get("error")})}
    m = _step(lambda: provider.fetch_metadata(provider_symbol))
    report["metadata"] = {"success": m["ok"], **({"exchange": m["value"].exchange, "currency": m["value"].currency, "timezone": m["value"].timezone} if m["ok"] else {"error_kind": m["error_kind"], "error": m["error"]})}
    report["success"] = True
    return report


def run_probe(symbols: list[str] | None = None, days: int = 60, pause_seconds: float = 1.5, provider: YahooFinanceProvider | None = None) -> dict[str, Any]:
    provider = provider or YahooFinanceProvider()
    entries = [e for e in APPROVED_EQUITIES if not symbols or e.symbol in symbols]
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "probe_id": str(uuid.uuid4()),
        "started_at": now,
        "provider": provider.descriptor.code,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "history_params": dict(HISTORY_PARAMS),
        "target_days": days,
        "symbols": [],
    }
    try:
        import yfinance

        report["yfinance_version"] = yfinance.__version__
    except Exception:  # noqa: BLE001
        report["yfinance_version"] = None
    for i, e in enumerate(entries):
        if i:
            time.sleep(pause_seconds)
        print(f"[{i + 1}/{len(entries)}] probing {e.symbol} ({e.provider_symbols['yahoo']}) ...", flush=True)
        report["symbols"].append(probe_symbol(provider, e.symbol, e.provider_symbols["yahoo"], days=days, now=now))
    report["finished_at"] = datetime.now(UTC)
    return report


def derive_capabilities(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Translate a probe report into capability statuses with evidence.

    VERIFIED needs every probed symbol to succeed. UNSUPPORTED needs positive evidence (e.g. identical
    Adj Close, or definitive 'no data' answers); transport failures never downgrade a capability.
    Anything the probe could not establish is UNKNOWN, and futures are never probed.
    """
    syms = report["symbols"]
    n = len(syms)

    def ok(path):
        return [s for s in syms if _dig(s, path)]

    def evidence(**kw):
        return {"probe_id": report["probe_id"], "symbols_probed": n, **kw}

    out: dict[str, dict[str, Any]] = {}
    five = ok(("five_minute", "success"))
    if n and len(five) == n:
        lag_values = [s["five_minute"]["last_bar_lag_minutes"] for s in five]
        out["HISTORICAL_INTRADAY_CANDLES"] = {"status": "VERIFIED", "note": "5m bars returned for every probed symbol",
            "evidence": evidence(candles=[s["five_minute"]["returned_candles"] for s in five], accepted_lookback_days=sorted({s["accepted_lookback_days"] for s in five}))}
        out["FIVE_MINUTE_INTERVAL"] = {"status": "VERIFIED", "note": f"requested {report['target_days']} days (minus a 10-minute safety margin)",
            "evidence": evidence(first_bar_ist=sorted({str(s["five_minute"]["first_bar_start_ist"]) for s in five}))}
        out["LATEST_CANDLE"] = {"status": "VERIFIED",
            "note": "latest bar is returned, and it can be the in-progress (partial) bar; finalisation is decided by Agent-X",
            "evidence": evidence(last_bar_lag_minutes=lag_values, in_progress_bar_returned=any(v < 0 for v in lag_values))}
    elif n and all(_dig(s, ("five_minute_attempts",)) and all(a.get("error_kind") in ("NO_DATA", "INVALID_REQUEST") for a in s["five_minute_attempts"]) for s in syms):
        out["FIVE_MINUTE_INTERVAL"] = {"status": "UNSUPPORTED", "note": "definitive no-data/invalid-request answers", "evidence": evidence()}
    for cap, key in (("HISTORICAL_DAILY_CANDLES", "daily"), ("CORPORATE_ACTIONS", "corporate_actions"),
                     ("QUOTE_RETRIEVAL", "quote"), ("INSTRUMENT_METADATA", "metadata")):
        good = ok((key, "success"))
        if n and len(good) == n:
            note = {"CORPORATE_ACTIONS": "dividends and splits at daily granularity; completeness not verified",
                    "QUOTE_RETRIEVAL": "last price and currency only; the provider gives no market timestamp",
                    "INSTRUMENT_METADATA": "exchange code, currency and timezone only; no instrument name"}.get(cap, "")
            out[cap] = {"status": "VERIFIED", "note": note, "evidence": evidence()}
    if five:
        differs = sum(s["five_minute"]["intraday_adj_close_differs_from_close"] for s in five)
        has_adj = all("adj_close" in s["five_minute"]["extras_present"] for s in five)
        if has_adj and differs == 0:
            out["ADJUSTED_INTRADAY_PRICES"] = {"status": "UNSUPPORTED",
                "note": "'Adj Close' is present in intraday responses but identical to 'Close' on every row: no intraday adjustment is offered",
                "evidence": evidence(rows_where_adj_close_differs=0)}
        elif differs:
            out["ADJUSTED_INTRADAY_PRICES"] = {"status": "UNKNOWN", "note": "adj_close differs from close on some intraday rows; not investigated", "evidence": evidence(rows=differs)}
    return out


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return None
        obj = obj[p]
    return obj


def derive_price_basis_observations(report: dict[str, Any]) -> dict[str, Any]:
    """Aggregated price-basis evidence. It never *applies* a verified label."""
    per = {s["symbol"]: s.get("price_basis", {}).get("classification") for s in report["symbols"]}
    tests = [t for s in report["symbols"] for t in s.get("price_basis", {}).get("split_tests", []) if "score" in t]
    return {
        "per_symbol": per,
        "split_tests_with_data": len(tests),
        "verdicts": sorted({t["verdict"] for t in tests}),
        "conclusion": (
            "Unadjusted daily 'Close' is continuous across split/bonus ex-dates (SPLIT_ADJUSTED_LIKE): Yahoo history is "
            "split-adjusted even with auto_adjust=False, so it is NOT as-traded. Intraday behaviour at a split is untested "
            "until a split occurs inside the archive window."
            if any(t["verdict"] == "SPLIT_ADJUSTED_LIKE" for t in tests)
            else "No split inside the daily range allowed a test; price basis remains unverified."
        ),
        "label_applied_to_stored_candles": "UNVERIFIED_PROVIDER_NATIVE",
    }


def summarize(report: dict[str, Any]) -> str:
    lines = [f"Probe {report['probe_id']}  yfinance {report.get('yfinance_version')}  python {report['python']}", ""]
    hdr = f"{'symbol':11}{'ok':4}{'lookback':9}{'candles':8}{'first (IST)':18}{'last (IST)':18}{'lagMin':7}{'absentDays':11}{'partial':8}{'missing':8}{'dup':4}{'bad':4}"
    lines.append(hdr)
    for s in report["symbols"]:
        f = s.get("five_minute", {})
        if not f.get("success"):
            lines.append(f"{s['symbol']:11}{'NO':4}  attempts={[(a['lookback_days'], a['error_kind']) for a in s['five_minute_attempts']]}")
            continue
        fmt = lambda v: v.strftime("%m-%d %H:%M") if isinstance(v, datetime) else str(v)
        bad = f["nan_price_rows"] + f["non_finite_rows"] + f["ohlc_relationship_violations"]
        lines.append(
            f"{s['symbol']:11}{'yes':4}{s['accepted_lookback_days']:<9}{f['returned_candles']:<8}"
            f"{fmt(f['first_bar_start_ist']):18}{fmt(f['last_bar_start_ist']):18}{f['last_bar_lag_minutes']:<7}"
            f"{len(f['absent_weekday_sessions']):<11}{len(f['partial_days']):<8}{f['missing_interval_total_excl_absent_days']:<8}{f['duplicate_timestamps']:<4}{bad:<4}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Opt-in Yahoo Finance probe (uses the network).")
    ap.add_argument("--symbols", help="comma-separated subset of the approved equities")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", default=None, help="directory for the JSON report (default: <project>/data/probe_reports)")
    ap.add_argument("--pause", type=float, default=1.5)
    args = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    report = run_probe(symbols, args.days, args.pause)
    out_dir = Path(args.out) if args.out else Path(__file__).resolve().parents[3] / "data" / "probe_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_{report['started_at'].strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(jsonable(report), indent=2), encoding="utf-8")
    print()
    print(summarize(report))
    print(f"\nFull report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
