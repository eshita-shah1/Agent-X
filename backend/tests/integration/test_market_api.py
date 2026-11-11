"""Market-data API: authentication, validation, user isolation, futures enforcement, jobs, provider health."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.enums import JobStatus, JobType, Timeframe
from app.db.models import Candle, DataIngestionJob, DataProvider, Instrument
from app.main import create_app
from app.marketdata.errors import ProviderUnavailable
from tests.conftest import STRONG_PASSWORD, auth_headers, login, register_user
from tests.support import NOW, WEEKDAYS, day_bars, days_bars

BASE = "/api/v1/market"
FUTURE_ID = "future-placeholder"


def ist_iso(day, h, m):
    return f"2026-11-{day:02d}T{h:02d}:{m:02d}:00+05:30"


@pytest.fixture
def ids(db, seeded):
    return {i.symbol: i.id for i in db.scalars(select(Instrument))}


@pytest.fixture
def user(make_market_user):
    return make_market_user()


def get(c, path, headers, **params):
    return c.get(BASE + path, headers=headers, params=params)


# ---- authentication ------------------------------------------------------------------------------------------------


PROTECTED = [
    ("GET", "/instruments"), ("GET", "/instruments/1"), ("GET", "/candles?instrument_id=1&start=2026-11-01T00:00:00%2B00:00&end=2026-11-02T00:00:00%2B00:00"),
    ("GET", "/coverage?instrument_id=1"), ("GET", "/ingestion/jobs"), (f"GET", f"/ingestion/jobs/{uuid.uuid4()}"),
    ("GET", "/providers"), ("GET", "/providers/health"), ("GET", "/providers/capabilities"),
    ("POST", "/ingestion/backfill"), ("POST", "/ingestion/incremental"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_market_endpoint_requires_authentication(market_client, seeded, method, path):
    r = market_client.request(method, BASE + path, json={} if method == "POST" else None)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method,path", PROTECTED[:3])
def test_a_forged_token_is_rejected(market_client, seeded, method, path):
    assert market_client.request(method, BASE + path, headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401


# ---- instruments ------------------------------------------------------------------------------------------------------


def test_the_instrument_list_has_ten_equities_and_two_disabled_futures(market_client, user, ids):
    r = get(market_client, "/instruments", user[1])
    assert r.status_code == 200 and len(r.json()) == 12
    eq = [i for i in r.json() if i["instrument_type"] == "EQUITY"]
    assert len(eq) == 10 and all(i["trading_status"] == "ENABLED" and i["currency"] == "INR" for i in eq)
    assert all(i["provider_symbols"] == [{"provider": "yahoo", "provider_symbol": f"{i['symbol']}.NS", "verification_status": "UNVERIFIED"}] for i in eq)
    fut = [i for i in r.json() if i["instrument_type"] == "FUTURE"]
    assert {i["symbol"] for i in fut} == {"NIFTY", "BANKNIFTY"}
    assert all(i["trading_status"] == "DISABLED" and i["disabled_reason"] and i["provider_symbols"] == [] and i["expiry_date"] is None for i in fut)


def test_instrument_filters(market_client, user, ids):
    assert len(get(market_client, "/instruments", user[1], type="FUTURE").json()) == 2
    assert len(get(market_client, "/instruments", user[1], enabled="true").json()) == 10
    assert {i["symbol"] for i in get(market_client, "/instruments", user[1], enabled="false").json()} == {"NIFTY", "BANKNIFTY"}
    assert get(market_client, "/instruments", user[1], type="BOND").status_code == 422


def test_a_single_instrument_and_a_missing_one(market_client, user, ids):
    ok = get(market_client, f"/instruments/{ids['RELIANCE']}", user[1])
    assert ok.status_code == 200 and ok.json()["symbol"] == "RELIANCE" and ok.json()["tick_size"] is None and ok.json()["lot_size"] is None
    assert get(market_client, "/instruments/999999", user[1]).status_code == 404
    assert get(market_client, "/instruments/abc", user[1]).status_code == 422


# ---- candles ------------------------------------------------------------------------------------------------------------


def archive_via_api(market_client, provider, user, days=WEEKDAYS):
    provider.bars = days_bars(days)
    r = market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={})
    assert r.status_code == 202, r.text
    return r.json()


def test_candles_require_a_bounded_timezone_aware_range(market_client, user, ids):
    h, i = user[1], ids["RELIANCE"]
    base = dict(instrument_id=i)
    assert get(market_client, "/candles", h, **base).status_code == 422  # start/end missing
    assert get(market_client, "/candles", h, **base, start="2026-11-02T00:00:00", end="2026-11-03T00:00:00").status_code == 422  # naive
    assert "UTC offset" in get(market_client, "/candles", h, **base, start="2026-11-02T00:00:00", end="2026-11-03T00:00:00+00:00").json()["detail"]
    assert get(market_client, "/candles", h, **base, start="2026-11-03T00:00:00+00:00", end="2026-11-03T00:00:00+00:00").status_code == 422
    assert get(market_client, "/candles", h, **base, start="2026-11-04T00:00:00+00:00", end="2026-11-03T00:00:00+00:00").status_code == 422
    assert get(market_client, "/candles", h, **base, start="2024-01-01T00:00:00+00:00", end="2026-11-03T00:00:00+00:00").status_code == 422  # > 366 days
    ok = dict(**base, start="2026-11-02T00:00:00+00:00", end="2026-11-03T00:00:00+00:00")
    assert get(market_client, "/candles", h, **ok, limit=0).status_code == 422
    assert get(market_client, "/candles", h, **ok, limit=2001).status_code == 422
    assert get(market_client, "/candles", h, **ok, series="weird").status_code == 422
    assert get(market_client, "/candles", h, **ok, timeframe="7m").status_code == 422
    assert get(market_client, "/candles", h, **ok, after="2026-11-02T00:00:00").status_code == 422
    assert get(market_client, "/candles", h, **ok).status_code == 200


def test_candles_for_unknown_and_disabled_instruments(market_client, user, ids):
    window = dict(start="2026-11-02T00:00:00+00:00", end="2026-11-03T00:00:00+00:00")
    assert get(market_client, "/candles", user[1], instrument_id=999999, **window).status_code == 404
    r = get(market_client, "/candles", user[1], instrument_id=ids["NIFTY"], **window)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INSTRUMENT_DISABLED"


def test_candles_are_served_with_provenance_and_the_price_basis_note(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:2])
    r = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(12, 0, 0), limit=500)
    body = r.json()
    assert r.status_code == 200 and body["count"] == 150 and body["next_cursor"] is None
    c = body["items"][0]
    assert (c["provider"], c["provider_symbol"], c["price_basis"], c["series"], c["is_canonical"], c["timeframe"]) == ("yahoo", "RELIANCE.NS", "UNVERIFIED_PROVIDER_NATIVE", "raw", True, "5m")
    assert c["bar_start_ts"].startswith("2026-11-02T03:45:00") and c["session_date"] == "2026-11-02" and c["ingestion_batch_id"]
    assert "split-adjusted" in body["price_basis_note"]


def test_candle_pagination_walks_the_whole_range_once(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:3])
    seen, cursor = [], None
    while True:
        params = dict(instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(12, 0, 0), limit=100)
        if cursor:
            params["after"] = cursor
        page = get(market_client, "/candles", user[1], **params).json()
        seen += [c["candle_id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 225 == len(set(seen))


def test_the_adjusted_series_is_empty_until_adjustments_exist(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:1])
    r = get(market_client, "/candles", user[1], instrument_id=ids["RELIANCE"], start=ist_iso(2, 0, 0), end=ist_iso(12, 0, 0), series="adjusted")
    assert r.status_code == 200 and r.json()["count"] == 0


# ---- coverage ---------------------------------------------------------------------------------------------------------------


def test_coverage_reports_actuals_never_assumptions(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user)
    c = get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], days=9, end_date="2026-11-11").json()
    assert c["candle_count"] == 600 and c["missing_intervals"] == 0 and c["days_complete"] == 8
    assert c["calendar"]["explicit_overrides_loaded"] == 0 and c["evaluated_until"].startswith("2026-11-11T11:00:00")
    assert "full_coverage_of_requested_range" in c and "unexplained_absences" in c and "probable_market_closures_inferred" in c


def test_coverage_rejects_a_timeframe_without_a_fixed_bar_length(market_client, user, ids):
    """Regression: `1d` used to reach the coverage calculation and fail with a 500."""
    for tf in ("5m", "15m", "60m"):
        assert get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], timeframe=tf, days=5).status_code == 200
    r = get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], timeframe="1d", days=5)
    assert r.status_code == 422 and "1d" in r.json()["detail"]


def test_reads_with_an_unregistered_active_provider_are_503_not_500(md_settings, registry, clock, seeded, ids):
    with TestClient(create_app(md_settings.model_copy(update={"marketdata_provider": "nobody"}), registry, clock)) as c:
        info = register_user(c)
        h = auth_headers(login(c, info["email"]))
        window = dict(instrument_id=ids["RELIANCE"], start="2026-11-02T00:00:00+00:00", end="2026-11-03T00:00:00+00:00")
        for path, params in (("/candles", window), ("/coverage", {"instrument_id": ids["RELIANCE"]})):
            r = c.get(BASE + path, headers=h, params=params)
            assert r.status_code == 503 and r.json()["detail"]["code"] == "PROVIDER_NOT_CONFIGURED", path


def test_coverage_of_an_empty_archive_claims_nothing(market_client, user, ids):
    c = get(market_client, "/coverage", user[1], instrument_id=ids["TCS"]).json()
    assert c["candle_count"] == 0 and c["full_coverage_of_requested_range"] is False and c["first_candle_start"] is None


def test_coverage_validation_and_disabled_futures(market_client, user, ids):
    assert get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], days=0).status_code == 422
    assert get(market_client, "/coverage", user[1], instrument_id=ids["RELIANCE"], days=401).status_code == 422
    assert get(market_client, "/coverage", user[1], instrument_id=999999).status_code == 404
    assert get(market_client, "/coverage", user[1], instrument_id=ids["BANKNIFTY"]).status_code == 409


# ---- ingestion jobs ------------------------------------------------------------------------------------------------------------


def test_a_backfill_job_runs_and_is_visible_to_its_owner(market_client, user, ids, provider, db):
    job = archive_via_api(market_client, provider, user)
    assert job["job_type"] == "BACKFILL" and job["timeframe"] == "5m" and job["parameters"]["source"] == "api" and job["parameters"]["days"] == 60
    assert len(job["parameters"]["instrument_ids"]) == 10 and ids["NIFTY"] not in job["parameters"]["instrument_ids"]
    done = get(market_client, f"/ingestion/jobs/{job['id']}", user[1]).json()
    assert done["status"] == "SUCCEEDED" and len(done["summary"]["instruments"]) == 10 and done["finished_at"]
    db.expire_all()
    assert db.query(Candle).count() == 6000


def test_an_incremental_job_can_be_started(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:2])
    r = market_client.post(BASE + "/ingestion/incremental", headers=user[1], json={"instrument_ids": [ids["RELIANCE"]]})
    assert r.status_code == 202 and r.json()["job_type"] == "INCREMENTAL"
    assert get(market_client, f"/ingestion/jobs/{r.json()['id']}", user[1]).json()["status"] == "SUCCEEDED"


def test_a_user_sees_only_their_own_jobs(market_client, provider, ids, make_market_user):
    a, b = make_market_user(), make_market_user()
    job_a = archive_via_api(market_client, provider, a, WEEKDAYS[:1])
    job_b = archive_via_api(market_client, provider, b, WEEKDAYS[:1])
    assert [j["id"] for j in get(market_client, "/ingestion/jobs", a[1]).json()["items"]] == [job_a["id"]]
    assert [j["id"] for j in get(market_client, "/ingestion/jobs", b[1]).json()["items"]] == [job_b["id"]]
    assert get(market_client, f"/ingestion/jobs/{job_a['id']}", b[1]).status_code == 404  # not 403: existence is not revealed
    assert get(market_client, f"/ingestion/jobs/{job_b['id']}", a[1]).status_code == 404


def test_cli_started_jobs_are_not_exposed_to_api_users(market_client, user, ids, db, provider):
    prov = db.scalar(select(DataProvider))
    cli_job = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=prov.id, timeframe=Timeframe.M5, requested_by_user_id=None)
    db.add(cli_job)
    db.commit()
    assert get(market_client, "/ingestion/jobs", user[1]).json()["items"] == []
    assert get(market_client, f"/ingestion/jobs/{cli_job.id}", user[1]).status_code == 404


def test_job_list_pagination(market_client, user, provider, ids, db):
    prov = db.scalar(select(DataProvider))
    uid = db.execute(select(DataIngestionJob.requested_by_user_id)).first()
    from app.db.models import User

    owner = db.scalar(select(User.id))
    for _ in range(5):
        db.add(DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.SUCCEEDED, provider_id=prov.id, timeframe=Timeframe.M5, requested_by_user_id=owner))
        db.commit()
    page = get(market_client, "/ingestion/jobs", user[1], limit=2, offset=0).json()
    assert page["count"] == 5 and len(page["items"]) == 2 and page["limit"] == 2
    assert len(get(market_client, "/ingestion/jobs", user[1], limit=2, offset=4).json()["items"]) == 1
    assert get(market_client, "/ingestion/jobs", user[1], limit=0).status_code == 422
    assert get(market_client, "/ingestion/jobs", user[1], limit=101).status_code == 422
    assert get(market_client, "/ingestion/jobs", user[1], offset=-1).status_code == 422


def test_a_second_ingestion_while_one_is_active_is_refused(market_client, user, ids, db, provider):
    prov = db.scalar(select(DataProvider))
    active = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.RUNNING, provider_id=prov.id, timeframe=Timeframe.M5, heartbeat_at=NOW)
    db.add(active)
    db.commit()
    r = market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INGESTION_ALREADY_RUNNING" and r.json()["detail"]["active_job_id"] == str(active.id)


def test_a_dead_active_job_does_not_block_new_work(market_client, user, ids, db, provider, clock):
    prov = db.scalar(select(DataProvider))
    dead = DataIngestionJob(job_type=JobType.BACKFILL, status=JobStatus.RUNNING, provider_id=prov.id, timeframe=Timeframe.M5, heartbeat_at=NOW - timedelta(hours=1))
    db.add(dead)
    db.commit()
    provider.bars = day_bars(WEEKDAYS[0])
    r = market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={"instrument_ids": [db.scalar(select(Instrument.id).where(Instrument.symbol == "ITC"))]})
    assert r.status_code == 202
    db.expire_all()
    assert db.get(DataIngestionJob, dead.id).status is JobStatus.INTERRUPTED


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY"])
def test_futures_can_never_be_ingested_through_the_api(market_client, user, ids, provider, symbol):
    for path in ("/ingestion/backfill", "/ingestion/incremental"):
        r = market_client.post(BASE + path, headers=user[1], json={"instrument_ids": [ids[symbol]]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "INSTRUMENT_DISABLED"
    assert provider.calls == []


def test_ingestion_request_validation(market_client, user, ids):
    post = lambda path, body: market_client.post(BASE + path, headers=user[1], json=body)  # noqa: E731
    assert post("/ingestion/backfill", {"days": 0}).status_code == 422
    assert post("/ingestion/backfill", {"days": 61}).status_code == 422  # beyond the approved 60-day target
    assert post("/ingestion/backfill", {"days": "x"}).status_code == 422
    assert post("/ingestion/backfill", {"instrument_ids": ["a"]}).status_code == 422
    assert post("/ingestion/backfill", {"instrument_ids": [999999]}).status_code == 404


def test_a_provider_outage_is_visible_on_the_job_and_in_provider_health(market_client, user, ids, provider):
    provider.fail_with = ProviderUnavailable("yahoo is down")
    r = market_client.post(BASE + "/ingestion/backfill", headers=user[1], json={"instrument_ids": [ids["RELIANCE"], ids["TCS"]]})
    job = get(market_client, f"/ingestion/jobs/{r.json()['id']}", user[1]).json()
    assert job["status"] == "FAILED" and all(x["error_kind"] == "UNAVAILABLE" for x in job["summary"]["instruments"])
    h = get(market_client, "/providers/health", user[1]).json()
    assert h["provider_status"] == "DEGRADED" and h["consecutive_failures"] == 2 and h["last_error_kind"] == "UNAVAILABLE"
    assert "yahoo is down" in h["last_error_message"] and h["overall_status"] in ("DEGRADED", "UNAVAILABLE")


# ---- providers -------------------------------------------------------------------------------------------------------------------


def test_the_provider_list_states_there_is_no_fallback(market_client, user, seeded):
    body = get(market_client, "/providers", user[1]).json()
    assert body["active_provider"] == "yahoo" and body["providers"][0]["code"] == "yahoo" and body["providers"][0]["configured"] is True
    assert "no automatic provider fallback" in body["note"]


def test_provider_health_before_any_use_is_unknown_not_healthy(market_client, user, ids):
    h = get(market_client, "/providers/health", user[1]).json()
    assert h["provider_status"] == "UNKNOWN" and h["provider_status_reason"] == "NO_ATTEMPTS_YET"
    assert len(h["instruments"]) == 10 and all(i["status"] == "UNAVAILABLE" and i["reason"] == "NO_ARCHIVED_DATA" for i in h["instruments"])
    assert h["overall_status"] == "UNAVAILABLE"  # no data is never reported as healthy
    assert all(t["status"] == "PROVISIONAL" for t in h["thresholds"].values())


def test_provider_health_after_a_successful_run_is_healthy_with_freshness_per_instrument(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user)
    h = get(market_client, "/providers/health", user[1]).json()
    assert h["provider_status"] == "HEALTHY" and h["consecutive_failures"] == 0 and h["last_success_at"] and h["last_observed_market_ts"]
    assert all(i["status"] == "HEALTHY" and i["latest_finalized_bar_end"] for i in h["instruments"]) and h["overall_status"] == "HEALTHY"


def test_capabilities_are_documented_or_unknown_until_a_probe_verifies_them(market_client, user, seeded):
    body = get(market_client, "/providers/capabilities", user[1]).json()
    caps = {c["capability"]: c for c in body["capabilities"]}
    assert body["provider"] == "yahoo" and set(body["legend"]) == {"DOCUMENTED", "VERIFIED", "UNSUPPORTED", "UNKNOWN"}
    assert not any(c["status"] == "VERIFIED" for c in caps.values())
    assert caps["FUTURES_CONTRACT_DATA"]["status"] in ("UNKNOWN",)
    assert get(market_client, "/providers/capabilities", user[1], provider="nobody").status_code == 404


# ---- boundaries ----------------------------------------------------------------------------------------------------------------------


def test_market_responses_never_expose_account_or_user_data(market_client, user, ids, provider):
    archive_via_api(market_client, provider, user, WEEKDAYS[:1])
    email = user[0]["email"]
    for path, params in [
        ("/instruments", {}), ("/providers", {}), ("/providers/health", {}), ("/providers/capabilities", {}), ("/ingestion/jobs", {}),
        ("/coverage", {"instrument_id": ids["RELIANCE"]}),
        ("/candles", {"instrument_id": ids["RELIANCE"], "start": ist_iso(2, 0, 0), "end": ist_iso(3, 0, 0)}),
    ]:
        text = get(market_client, path, user[1], **params).text
        assert "initial_capital" not in text and email not in text and "password" not in text.lower(), path


def test_creating_the_application_starts_no_ingestion(md_settings, registry, clock, db, seeded):
    with TestClient(create_app(md_settings, registry, clock)):
        pass
    assert registry.get("yahoo").calls == []  # no provider call on start-up, no scheduler, no agent
    db.expire_all()
    assert db.query(DataIngestionJob).count() == 0


def test_the_phase_1_endpoints_are_unaffected(market_client, user):
    assert market_client.get("/health").json() == {"status": "ok"}
    assert market_client.get("/api/v1/account", headers=user[1]).status_code == 200
    assert market_client.get("/api/v1/auth/me", headers=user[1]).status_code == 200
