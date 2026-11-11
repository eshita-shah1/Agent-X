"""Phase 2 finalization: the owner-approved decisions, checked end-to-end against the test database.

Everything here uses the fake provider and the disposable test database; the real archive is never touched.
"""

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.enums import JobStatus, JobType, Timeframe
from app.db.models import Candle, DataIngestionJob, DataProvider, DataRestatementEvent, Instrument
from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.errors import ProviderUnavailable
from app.marketdata.types import RawBar
from tests.integration.test_market_api import BASE, archive_via_api, get, ist_iso
from tests.support import NOW, WEEKDAYS, day_bars, days_bars

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


@pytest.fixture
def ids(db, seeded):
    return {i.symbol: i.id for i in db.scalars(select(Instrument))}


@pytest.fixture
def user(make_market_user):
    return make_market_user()


@pytest.fixture
def job_id(db, svc):
    prov = db.scalar(select(DataProvider))
    job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.RUNNING, provider_id=prov.id, timeframe=Timeframe.M5)
    db.add(job)
    db.commit()
    return job.id


def ist(day, h, m, s=0):
    return datetime(2026, 11, day, h, m, s, tzinfo=EXCHANGE_TZ)


def ingest(svc, db, job_id, start, end, symbol="RELIANCE"):
    inst = db.scalar(select(Instrument).where(Instrument.symbol == symbol))
    return svc.ingest_window(job_id=job_id, instrument=inst, provider_code="yahoo", timeframe="5m", start=start, end=end)


def timestamps(obj, path=""):
    """Every ISO date-time string in a JSON document, with its location."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from timestamps(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from timestamps(v, f"{path}[{i}]")
    elif isinstance(obj, str) and ISO.match(obj):
        yield path, obj


# ---- A. forming vs finalized ------------------------------------------------------------------------------------------


def test_ingestion_respects_the_sixty_second_margin_and_never_archives_the_forming_bar(db, svc, provider, clock, job_id):
    assert svc.thresholds.finalization_margin == timedelta(seconds=60)  # the approved default, unmodified by fixtures
    provider.bars = day_bars(WEEKDAYS[-1], last=11)  # 09:15 .. 10:05 (the 10:05 bar is forming until 10:10)
    start = ist(11, 0, 0).astimezone(UTC)
    clock.now = ist(11, 10, 5, 59).astimezone(UTC)  # 10:00 bar ended 59 s ago
    first = ingest(svc, db, job_id, start, clock.now)
    assert (first.candles_inserted, first.non_final_dropped) == (9, 2)
    clock.now = ist(11, 10, 6, 0).astimezone(UTC)  # exactly end + 60 s
    second = ingest(svc, db, job_id, start, clock.now)
    assert (second.candles_inserted, second.non_final_dropped) == (1, 1)
    starts = list(db.scalars(select(Candle.bar_start_ts).order_by(Candle.bar_start_ts)))
    assert starts[-1] == ist(11, 10, 0).astimezone(UTC) and ist(11, 10, 5).astimezone(UTC) not in starts
    assert all(c.is_final and c.observed_at >= c.bar_end_ts + timedelta(seconds=60) for c in db.scalars(select(Candle)))


def test_the_candle_api_marks_every_served_candle_final_and_omits_forming_bars(market_client, user, ids, provider, clock):
    provider.bars = day_bars(WEEKDAYS[-1], last=11)
    clock.now = ist(11, 10, 7).astimezone(UTC)
    assert market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={"instrument_ids": [ids["RELIANCE"]]}).status_code == 202
    body = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(11, 0, 0), end=ist_iso(12, 0, 0)).json()
    assert body["count"] == 10 and all(c["is_final"] is True for c in body["items"])
    assert body["items"][-1]["bar_start_ts"] == "2026-11-11T04:30:00Z"  # 10:00 IST; the forming 10:05 bar is absent


# ---- B. execution-price restrictions ------------------------------------------------------------------------------------


def test_served_prices_are_never_labelled_execution_ready(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:1])
    body = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(3, 0, 0)).json()
    assert {c["price_basis"] for c in body["items"]} == {"UNVERIFIED_PROVIDER_NATIVE"}
    assert "No price here is classified as execution-ready" in body["price_basis_note"] and "Phase 4" in body["price_basis_note"]
    assert not any(k for c in body["items"] for k in c if "execution" in k or "tradable" in k)


# ---- C. restatements: first observation stays canonical ---------------------------------------------------------------


def _bump(bars, by):
    return [RawBar(b.start_ts, b.open + by, b.high + by, b.low + by, b.close + by, b.volume) for b in bars]


def test_first_observation_stays_canonical_through_multiple_restatements(db, svc, provider, clock, job_id):
    original = day_bars(WEEKDAYS[-2], last=3)
    start, versions = ist(10, 0, 0).astimezone(UTC), []
    for by in (0, 1, 2, 0, 1):  # original, two distinct restatements, then both seen again
        provider.bars = _bump(original, by)
        clock.advance(minutes=10)
        versions.append(ingest(svc, db, job_id, start, clock.now))
    assert [v.restatements for v in versions] == [0, 3, 3, 0, 0] and [v.candles_inserted for v in versions] == [3, 0, 0, 0, 0]
    slot = original[0].start_ts.astimezone(UTC)
    rows = db.scalars(select(Candle).where(Candle.bar_start_ts == slot).order_by(Candle.observation_version)).all()
    assert [(r.observation_version, r.is_canonical) for r in rows] == [(1, True), (2, False), (3, False)]
    assert [r.close for r in rows] == [Decimal(repr(original[0].close)).quantize(Decimal("0.000001")) + by for by in (0, 1, 2)]
    assert rows[0].observed_at < rows[1].observed_at < rows[2].observed_at  # each observation keeps its own time
    assert len({r.ingestion_batch_id for r in rows}) == 3  # and its own provenance
    events = db.scalars(select(DataRestatementEvent).where(DataRestatementEvent.bar_start_ts == slot)).all()
    assert {e.original_candle_id for e in events} == {rows[0].id}  # every restatement points at the first observation
    served, _ = svc.get_candles(instrument_id=rows[0].instrument_id, timeframe="5m", start=slot, end=slot + timedelta(minutes=5), limit=10)
    assert [(c["candle_id"], c["close"], c["is_canonical"]) for c in served] == [(rows[0].id, rows[0].close, True)]


def test_canonical_selection_does_not_depend_on_which_value_is_seen_first_later(db, svc, provider, clock, job_id):
    """Two instruments see the same restatements in a different order; each keeps its own first observation."""
    base = day_bars(WEEKDAYS[-2], last=1)
    start = ist(10, 0, 0).astimezone(UTC)
    for symbol, order in (("RELIANCE", (0, 1, 2)), ("TCS", (0, 2, 1))):
        for by in order:
            provider.bars = _bump(base, by)
            clock.advance(minutes=1)
            ingest(svc, db, job_id, start, clock.now, symbol)
    canon = dict(
        db.execute(select(Instrument.symbol, Candle.close).join(Instrument).where(Candle.is_canonical)).all()
    )
    assert canon["RELIANCE"] == canon["TCS"]
    assert db.scalar(select(func.count()).select_from(Candle).where(~Candle.is_canonical)) == 4


# ---- D. gaps and inferred closures ----------------------------------------------------------------------------------------


def test_coverage_labels_inferred_closures_and_keeps_other_absences_unexplained(market_client, user, ids, provider, clock):
    days = [d for d in WEEKDAYS if d not in (date(2026, 11, 10),)]
    provider.bars = days_bars(days)
    assert market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={}).status_code == 202
    c = get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], days=9, end_date="2026-11-11").json()
    assert c["probable_market_closures_inferred"] == ["2026-11-10"] and c["unexplained_absences"] == []
    assert c["calendar"]["authoritative_holiday_source"] is None and c["calendar"]["inferred_closures_are_verified_holidays"] is False
    assert c["calendar"]["explicit_overrides_loaded"] == 0 and "inferred closures" in c["notes"][0] and "not verified" in c["notes"][0]


# ---- E. thresholds do not gate anything ----------------------------------------------------------------------------------


def test_stale_data_and_an_unavailable_provider_change_reporting_only(market_client, user, ids, provider, clock):
    archive_via_api(market_client, provider, user, WEEKDAYS)
    clock.now = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)  # Friday 11:30 IST; archive ends Wednesday's close
    provider.fail_with = ProviderUnavailable("down")
    three = [ids[s] for s in ("RELIANCE", "TCS", "SBIN")]
    assert market_client.post(BASE + "/ingestion/incremental", headers=user[1], json={"instrument_ids": three}).status_code == 202
    h = get(market_client, "/providers/health", user[1]).json()
    assert h["provider_status"] == "UNAVAILABLE" and h["consecutive_failures"] == 3
    assert {i["status"] for i in h["instruments"]} == {"STALE"} and h["overall_status"] == "UNAVAILABLE"
    assert all(i["data_age_seconds"] > i["lag_seconds"] > 15 * 60 for i in h["instruments"])
    # reads and operator-initiated ingestion are still allowed: nothing is gated by the thresholds
    r = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(12, 0, 0), limit=1000)
    assert r.status_code == 200 and r.json()["count"] == 600
    provider.fail_with = None
    provider.bars = days_bars(WEEKDAYS + [date(2026, 11, 12)])
    job = market_client.post(BASE + "/ingestion/incremental", headers=user[1], json={"instrument_ids": three})
    assert job.status_code == 202
    assert get(market_client, f"/ingestion/jobs/{job.json()['id']}", user[1]).json()["status"] == "SUCCEEDED"
    assert get(market_client, "/providers/health", user[1]).json()["provider_status"] == "HEALTHY"


# ---- F. scheduling and local permissions -------------------------------------------------------------------------------------


def test_starting_the_application_runs_no_ingestion(market_client, seeded, provider, db):
    assert market_client.get("/health").status_code == 200
    assert provider.calls == [] and db.scalar(select(func.count()).select_from(DataIngestionJob)) == 0


def test_any_authenticated_user_may_trigger_ingestion_locally(market_client, ids, provider, make_market_user):
    provider.bars = day_bars(WEEKDAYS[0])
    for headers in (make_market_user()[1], make_market_user()[1]):  # two ordinary users, no roles involved
        r = market_client.post(BASE + "/ingestion/backfill", headers=headers, json={"instrument_ids": [ids["ITC"]]})
        assert r.status_code == 202 and get(market_client, f"/ingestion/jobs/{r.json()['id']}", headers).json()["status"] == "SUCCEEDED"
    assert market_client.post(BASE + "/ingestion/backfill", json={}).status_code == 401


# ---- G. precision --------------------------------------------------------------------------------------------------------------


def test_float32_provider_noise_survives_to_the_api_unrounded(market_client, user, ids, provider):
    b = day_bars(WEEKDAYS[0], last=1)[0]
    provider.bars = [RawBar(b.start_ts, 1338.5999755859375, 1340.0999755859375, 1337.0, 1338.5999755859375, 10.0)]
    market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={"instrument_ids": [ids["RELIANCE"]]})
    c = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(3, 0, 0)).json()["items"][0]
    assert (c["open"], c["high"], c["low"]) == ("1338.599976", "1340.099976", "1337.000000")


# ---- H. UTC -----------------------------------------------------------------------------------------------------------------------


def test_every_api_timestamp_is_utc_with_a_z_suffix(market_client, user, ids, provider):
    job = archive_via_api(market_client, provider, user, WEEKDAYS[:1])
    docs = {
        "job": get(market_client, f"/ingestion/jobs/{job['id']}", user[1]).json(),
        "jobs": get(market_client, "/ingestion/jobs", user[1]).json(),
        "candles": get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(3, 0, 0)).json(),
        "coverage": get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], days=10, end_date="2026-11-11").json(),
        "health": get(market_client, "/providers/health", user[1]).json(),
        "account": market_client.get("/api/v1/account", headers=user[1]).json(),  # Phase 1 endpoint
    }
    found = [(name, p, v) for name, d in docs.items() for p, v in timestamps(d)]
    assert {name for name, _, _ in found} >= {"job", "jobs", "candles", "coverage", "health", "account"}
    assert [(n, p, v) for n, p, v in found if not v.endswith("Z")] == []


def test_candle_instants_are_utc_while_the_session_date_is_exchange_local(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:1])
    items = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(3, 0, 0)).json()["items"]
    first, last = items[0], items[-1]
    assert (first["bar_start_ts"], first["bar_end_ts"]) == ("2026-11-02T03:45:00Z", "2026-11-02T03:50:00Z")  # 09:15-09:20 IST
    assert (last["bar_start_ts"], last["bar_end_ts"]) == ("2026-11-02T09:55:00Z", "2026-11-02T10:00:00Z")  # 15:25-15:30 IST
    assert {c["session_date"] for c in items} == {"2026-11-02"}
    parsed = datetime.fromisoformat(first["bar_start_ts"].replace("Z", "+00:00"))
    assert parsed.astimezone(EXCHANGE_TZ).strftime("%H:%M") == "09:15" and parsed == NOW.replace(day=2, hour=3, minute=45)
