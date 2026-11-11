"""Phase 2 finalization: the owner-approved decisions, checked without a database or the network.

D1 execution price deferred · D3 provisional thresholds · D4 inferred closures · D5 operator-initiated ingestion ·
D8 tick-size normalisation deferred.
"""

import pathlib
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.core.config import Settings
from app.db.enums import PriceBasis
from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.marketdata.freshness import Thresholds, assess_freshness, assess_provider
from app.marketdata.gaps import probable_closures
from app.marketdata.pipeline import process_bars, to_decimal
from app.marketdata.registry import ALL_ENTRIES
from app.marketdata.service import thresholds_from
from app.marketdata.types import DataStatus, IssueType, ProviderBarsResult, RawBar

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
CAL = TradingCalendar()
APPROVED_MARGIN = timedelta(seconds=60)


def app_sources():
    return {p: p.read_text(encoding="utf-8") for p in APP.rglob("*.py")}


def ist(h, m, s=0, day=11):
    return datetime(2026, 11, day, h, m, s, tzinfo=EXCHANGE_TZ)


def run(bars, observed_at, request_end=None):
    res = ProviderBarsResult("yahoo", "X.NS", "5m", observed_at - timedelta(days=1), request_end or observed_at, bars, observed_at)
    return process_bars(res, instrument_id=1, timeframe="5m", calendar=CAL, finalization_margin=APPROVED_MARGIN)


def bar(ts, p=100.0):
    return RawBar(ts, p, p + 1, p - 1, p, 1000.0)


# ---- D3: provisional thresholds ----------------------------------------------------------------------------------


def test_the_approved_provisional_threshold_defaults():
    f = Settings.model_fields
    assert f["marketdata_finalization_margin_seconds"].default == 60
    assert f["marketdata_stale_after_minutes"].default == 15
    assert f["marketdata_provider_unavailable_after_failures"].default == 3


def test_the_thresholds_are_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("MARKETDATA_FINALIZATION_MARGIN_SECONDS", "90")
    monkeypatch.setenv("MARKETDATA_STALE_AFTER_MINUTES", "30")
    monkeypatch.setenv("MARKETDATA_PROVIDER_UNAVAILABLE_AFTER_FAILURES", "5")
    t = thresholds_from(Settings())  # type: ignore[call-arg]
    assert t == Thresholds(timedelta(seconds=90), timedelta(minutes=30), 5)


def test_provider_becomes_unavailable_at_exactly_three_consecutive_failures():
    th = Thresholds(APPROVED_MARGIN, timedelta(minutes=15), 3)

    def state(n):
        return SimpleNamespace(
            last_attempt_at=ist(10, 0), last_success_at=None, last_failure_at=ist(10, 0), last_error_kind="UNAVAILABLE",
            last_error_message="x", consecutive_failures=n, last_observed_market_ts=None, last_successful_ingestion_at=None,
        )

    assert [assess_provider(state(n), th).status for n in (0, 1, 2, 3, 4)] == [
        DataStatus.HEALTHY, DataStatus.DEGRADED, DataStatus.DEGRADED, DataStatus.UNAVAILABLE, DataStatus.UNAVAILABLE,
    ]


def test_stale_boundary_is_fifteen_minutes_of_lag_and_age_is_reported_separately():
    th = Thresholds(APPROVED_MARGIN, timedelta(minutes=15), 3)
    now = ist(13, 6)  # expected latest bar end: 13:05 (13:05 + 60 s <= 13:06)
    at_threshold = assess_freshness(latest_bar_end=ist(12, 50), now=now, calendar=CAL, timeframe="5m", thresholds=th)
    beyond = assess_freshness(latest_bar_end=ist(12, 45), now=now, calendar=CAL, timeframe="5m", thresholds=th)
    assert (at_threshold.status, at_threshold.lag_seconds) == (DataStatus.DEGRADED, 900.0)
    assert (beyond.status, beyond.lag_seconds) == (DataStatus.STALE, 1200.0)
    # the observed age (now - latest bar end) is a separate fact from the lag measured against the threshold
    assert at_threshold.data_age_seconds == 16 * 60 and beyond.data_age_seconds == 21 * 60


def test_freshness_assessment_is_used_only_for_reporting():
    """D3: thresholds describe data. Nothing outside the reporting path consumes them, so they cannot gate anything."""
    users = {p.relative_to(APP).as_posix() for p, t in app_sources().items() if re.search(r"\bassess_(freshness|provider)\b", t)}
    assert users == {"marketdata/freshness.py", "marketdata/service.py"}


# ---- forming vs finalized (A) ---------------------------------------------------------------------------------------


def test_finalization_boundary_with_the_approved_sixty_second_margin():
    end = ist(10, 5)
    assert run([bar(ist(10, 0))], observed_at=end + timedelta(seconds=59)).candles == []
    assert len(run([bar(ist(10, 0))], observed_at=end + timedelta(seconds=60)).candles) == 1


def test_the_forming_bar_is_never_archived_even_long_after_its_start():
    # observed at 10:09:59: the 10:05 bar is still forming; the 10:00 bar ended 4:59 ago and is final
    p = run([bar(ist(10, 0)), bar(ist(10, 5))], observed_at=ist(10, 9, 59))
    assert [c.bar_start_ts for c in p.candles] == [ist(10, 0).astimezone(UTC)]
    assert p.non_final_dropped == 1 and [i.issue_type for i in p.issues] == [IssueType.INCOMPLETE_CANDLE]


def test_a_bar_cut_off_by_the_request_end_is_not_final_even_when_observed_late():
    p = run([bar(ist(10, 0))], observed_at=ist(16, 0), request_end=ist(10, 3))
    assert p.candles == [] and p.issues[0].details["ends_after_request_end"] is True


def test_every_archived_candle_is_flagged_final():
    assert all(c.bar_end_ts + APPROVED_MARGIN <= c.observed_at for c in run([bar(ist(10, 0)), bar(ist(10, 5))], observed_at=ist(16, 0)).candles)


# ---- D1: execution price deferred (B) -------------------------------------------------------------------------------


def test_no_application_code_assigns_a_verified_price_basis():
    for path, text in app_sources().items():
        if path.name == "enums.py":
            continue
        assert not re.search(r"AS_TRADED_VERIFIED|SPLIT_ADJUSTED_VERIFIED", text), path.name
    assert PriceBasis.UNVERIFIED_PROVIDER_NATIVE.value == "UNVERIFIED_PROVIDER_NATIVE"


def test_no_mock_exchange_order_or_execution_code_exists():
    # Phase 3 (decision D19) permits app/indicators, app/strategies and app/backtest; every other ban stays.
    forbidden_dirs = {"exchange", "mock_exchange", "orders", "execution", "risk", "agent"}
    assert not [p for p in APP.iterdir() if p.is_dir() and p.name in forbidden_dirs]
    for path, text in app_sources().items():
        assert not re.search(r"\b(execution_price|fill_price|place_order|submit_order)\b", text), path.name


# ---- D4: inferred closures (D) --------------------------------------------------------------------------------------


def test_the_calendar_ships_with_no_holidays_and_says_so():
    assert CAL.known_overrides == 0 and "no holiday source approved" in CAL.source
    assert not CAL.is_known_holiday(date(2026, 10, 2))  # a closure inferred from the archive is still not a holiday


def test_an_inferred_closure_does_not_change_the_calendar():
    day = date(2026, 11, 10)
    assert probable_closures({"a": [day], "b": [day]}, [day]) == [day]
    assert CAL.session(day) is not None  # still an expected regular session; the inference is reported, not applied


# ---- D5: no scheduler (F) ---------------------------------------------------------------------------------------------


def test_nothing_starts_background_work_on_application_start_up():
    main = (APP / "main.py").read_text(encoding="utf-8")
    lifespan = main[main.index("async def lifespan") : main.index("app = FastAPI")]
    assert re.sub(r"\s+", " ", lifespan).strip().endswith("yield app.state.engine.dispose()")
    for path, text in app_sources().items():
        assert not re.search(r"apscheduler|BackgroundScheduler|on_event\(|threading\.Thread|asyncio\.create_task|sched\.scheduler", text), path.name


def test_the_requirements_contain_no_scheduler():
    reqs = (APP.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "apscheduler" not in reqs and "celery" not in reqs


# ---- D8: tick size deferred (G) -----------------------------------------------------------------------------------------


def test_no_tick_size_table_or_guessed_tick_size_exists():
    for e in ALL_ENTRIES:
        assert getattr(e, "tick_size", None) is None
    for path, text in app_sources().items():
        assert not re.search(r"tick_size\s*[:=]\s*(Decimal\()?['\"]?\d", text), path.name
        assert not re.search(r"TICK_SIZES?\b", text), path.name


def test_provider_float_precision_is_preserved_not_snapped_to_a_tick():
    # a real Yahoo float32 value for 1338.60: kept to 6 dp, not rounded to 2 dp or to a 0.05/0.10 tick
    assert to_decimal(1338.5999755859375) == Decimal("1338.599976")
    p = run([RawBar(ist(10, 0), 1338.5999755859375, 1340.0999755859375, 1337.0, 1338.5999755859375, 10.0)], observed_at=ist(16, 0))
    assert (p.candles[0].open, p.candles[0].high) == (Decimal("1338.599976"), Decimal("1340.099976"))
