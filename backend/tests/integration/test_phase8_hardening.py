"""Phase 8 on PostgreSQL: HTTP hardening, sessions and logout, login lockout, operator-only ingestion, readiness with
migration compatibility, system status, agent observability, backups of the test database, the migration guard."""

import os
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app.core.security import create_access_token
from app.db.enums import AgentCycleStatus, AgentStatus
from app.db.models import AgentCycle, AuditEvent, User
from app.main import create_app
from app.monitoring import health
from app.ops import backup
from tests.conftest import BACKEND_DIR, STRONG_PASSWORD, auth_headers, login, register_user
from tests.integration.agent_support import World


def app_with(settings, **over):
    return create_app(settings.model_copy(update=over))


# ---- HTTP hardening -------------------------------------------------------------------------------------------


def test_security_headers_and_request_ids(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer" and len(r.headers["x-request-id"]) == 32
    assert "strict-transport-security" not in r.headers  # production only (TLS at the proxy)
    api = client.get("/api/v1/auth/me", headers={"X-Request-ID": "trace-123"})
    assert api.status_code == 401 and api.headers["x-request-id"] == "trace-123"
    assert api.headers["cache-control"] == "no-store" and "default-src 'none'" in api.headers["content-security-policy"]
    bad = client.get("/health", headers={"X-Request-ID": "x" * 65 + "<script>"})
    assert bad.headers["x-request-id"] != "x" * 65 + "<script>" and len(bad.headers["x-request-id"]) == 32


def test_oversized_bodies_are_refused_before_reaching_the_endpoint(test_settings):
    with TestClient(app_with(test_settings, max_request_body_bytes=2048)) as c:
        big = {"email": "a@example.com", "password": "x" * 5000, "initial_capital": "1"}
        r = c.post("/api/v1/auth/register", json=big)
        assert r.status_code == 413 and r.json()["request_id"]

        def chunks():
            yield b'{"email": "a@example.com", "password": "'
            for _ in range(10):
                yield b"x" * 500
            yield b'", "initial_capital": "1"}'

        r = c.post("/api/v1/auth/register", content=chunks(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413
    with TestClient(app_with(test_settings)) as c:
        assert c.post("/api/v1/auth/register", json={"email": "ok@example.com", "password": STRONG_PASSWORD,
                                                     "initial_capital": "1000"}).status_code == 201


def test_unhandled_errors_return_a_safe_json_500(test_settings):
    app = app_with(test_settings)
    boom = APIRouter()

    @boom.get("/boom")
    def _boom():
        raise RuntimeError("internal detail password=hunter2")

    app.include_router(boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and r.json()["detail"] == "Internal server error" and r.json()["request_id"]
    assert "hunter2" not in r.text and "RuntimeError" not in r.text


def test_api_docs_follow_the_environment(test_settings):
    with TestClient(app_with(test_settings, enable_api_docs=False)) as c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    with TestClient(app_with(test_settings)) as c:
        assert c.get("/openapi.json").status_code == 200


def test_cors_only_for_listed_origins(test_settings):
    pre = {"Origin": "https://app.example", "Access-Control-Request-Method": "GET"}
    with TestClient(app_with(test_settings)) as c:
        assert "access-control-allow-origin" not in c.options("/api/v1/auth/me", headers=pre).headers
    with TestClient(app_with(test_settings, cors_allow_origins=["https://app.example"])) as c:
        ok = c.options("/api/v1/auth/me", headers=pre)
        assert ok.headers["access-control-allow-origin"] == "https://app.example"
        other = c.options("/api/v1/auth/me", headers={**pre, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


# ---- sessions, logout, lockout ----------------------------------------------------------------------------------


def test_logout_revokes_every_existing_token(client):
    info = register_user(client)
    first, second = login(client, info["email"]), None
    time.sleep(1.1)  # tokens carry 1-second issued-at precision
    second = login(client, info["email"])
    assert client.get("/api/v1/auth/me", headers=auth_headers(first)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=auth_headers(second)).status_code == 204
    for token in (first, second):
        assert client.get("/api/v1/auth/me", headers=auth_headers(token)).status_code == 401
        assert client.get("/api/v1/journal", headers=auth_headers(token)).status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 401
    time.sleep(1.1)
    fresh = login(client, info["email"])
    assert client.get("/api/v1/auth/me", headers=auth_headers(fresh)).status_code == 200


def test_repeated_failed_logins_lock_the_email_without_revealing_accounts(client, db):
    info = register_user(client)
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"email": info["email"], "password": "wrong-password-1"}).status_code == 401
    locked = client.post("/api/v1/auth/login", json={"email": info["email"], "password": STRONG_PASSWORD})
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) == 15 * 60
    other = register_user(client)
    assert client.post("/api/v1/auth/login", json={"email": other["email"], "password": STRONG_PASSWORD}).status_code == 200
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-1"})
    assert client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}).status_code == 429
    # the failures live in the append-only audit log (they cannot be rewritten to unlock an account) ...
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "auth.login_failed")) == 10
    # ... and the threshold is configuration
    with TestClient(create_app(client.app.state.settings.model_copy(update={"login_max_failures": 6}))) as c:
        assert c.post("/api/v1/auth/login", json={"email": info["email"], "password": STRONG_PASSWORD}).status_code == 200


def test_deactivated_users_are_refused(client, db):
    info = register_user(client)
    token = login(client, info["email"])
    db.execute(update(User).where(User.email == info["email"]).values(is_active=False))
    db.commit()
    assert client.get("/api/v1/auth/me", headers=auth_headers(token)).status_code == 401


# ---- authorization ------------------------------------------------------------------------------------------------


def test_ingestion_is_operator_only_where_configured(test_settings, db):
    with TestClient(app_with(test_settings, ingestion_requires_operator=True)) as c:
        info = register_user(c)
        h = auth_headers(login(c, info["email"]))
        r = c.post("/api/v1/market/ingestion/backfill", json={"instrument_ids": [1], "days": 1}, headers=h)
        assert r.status_code == 403
        assert db.scalar(select(AuditEvent).where(AuditEvent.event_type == "operator.access_denied")) is not None
        db.execute(update(User).where(User.email == info["email"]).values(is_operator=True))
        db.commit()
        r = c.post("/api/v1/market/ingestion/backfill", json={"instrument_ids": [1], "days": 1}, headers=h)
        assert r.status_code != 403  # past authorization (here: no such instrument/provider)


@pytest.mark.parametrize("method, path", [
    ("get", "/api/v1/system/status"), ("get", "/api/v1/system/agents"), ("post", "/api/v1/auth/logout"),
])
def test_phase8_routes_require_authentication(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ---- readiness and status ---------------------------------------------------------------------------------------


def test_readiness_requires_the_schema_at_this_code_s_head(client, monkeypatch):
    assert client.get("/health/ready").json() == {"status": "ready"}
    check = health.database_check(client.app.state.engine)
    assert check["migration"]["compatible"] and check["migration"]["current"] == health.expected_head()
    monkeypatch.setattr(health, "expected_head", lambda: "9999")
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}  # nothing more is exposed publicly
    assert client.get("/health").status_code == 200  # liveness unaffected


def test_system_status_separates_readiness_from_reported_conditions(client):
    info = register_user(client)
    r = client.get("/api/v1/system/status", headers=auth_headers(login(client, info["email"])))
    body = r.json()
    assert r.status_code == 200 and body["overall"] == "ok" and body["readiness"]["migration"]["compatible"]
    assert body["reported"]["execution"]["enabled"] is False and body["reported"]["execution"]["is_failure"] is False
    assert body["reported"]["llm"]["enabled"] is False and body["reported"]["llm"]["affects_readiness"] is False
    assert body["reported"]["agent"] is None
    text_ = r.text.lower()
    assert "password" not in text_ and "secret" not in text_ and "jwt" not in text_


def test_agent_observability_flags_stale_workers_and_failed_cycles(db, engine, test_settings):
    w = World(db, engine, test_settings, execution=False)
    w.opening_range()
    w.at("09:30")
    w.configure()
    w.start()
    w.cycle()
    agent = w.agent()
    for status_ in (AgentCycleStatus.FAILED, AgentCycleStatus.INTERRUPTED):
        db.add(AgentCycle(user_id=agent.user_id, agent_id=agent.id, status=status_, worker_id="w", agent_status=AgentStatus.RUNNING,
                          session_date=w.day, started_at=w.clock(), finished_at=w.clock(), error_code="X"))
    db.commit()
    w.clock.now += timedelta(seconds=test_settings.agent_worker_stale_after_seconds + 60)
    operator = db.scalar(select(User).where(User.id == agent.user_id))
    operator.is_operator = True
    db.commit()
    app = create_app(w.settings, clock=w.clock)
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {create_access_token(agent.user_id, w.settings)}"}
        fleet = c.get("/api/v1/system/agents", headers=h).json()
        [row] = fleet["agents"]
        assert row["status"] == "RUNNING" and row["mode"] == "SIGNALS_ONLY" and row["worker_stale"] is True
        assert row["last_successful_cycle_at"] is not None and row["last_heartbeat_at"] is not None
        assert row["cycles_24h"]["FAILED"] == 1 and row["cycles_24h"]["INTERRUPTED"] == 1
        assert "WORKER_NOT_SEEN" in row["attention"] and fleet["needing_attention"] == [agent.id]
        mine = c.get("/api/v1/system/status", headers=h).json()["reported"]["agent"]
        assert mine["agent_id"] == agent.id and mine["worker_stale"] is True
        db.execute(update(User).where(User.id == agent.user_id).values(is_operator=False))
        db.commit()
        assert c.get("/api/v1/system/agents", headers=h).status_code == 403


def test_monitoring_cli_reports_and_exits_by_severity(test_settings, monkeypatch, capsys):
    from app.monitoring import cli

    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    code = cli.main(["health"])
    out = capsys.readouterr().out
    assert code == 0 and '"compatible": true' in out and '"is_failure": false' in out


# ---- backups of the TEST database and the migration guard ----------------------------------------------------------


def test_backup_manifest_verification_detects_tampering(test_settings, client, tmp_path):
    register_user(client)  # some data
    m = backup.create_backup(test_settings, tmp_path, label="pytest")
    dump = tmp_path / m["file"]
    assert m["database"] == test_settings.db_test_name and m["alembic_version"] == health.expected_head()
    assert m["snapshot"]["users"]["rows"] == 1 and "users" in m["tables_with_data"]
    assert backup.verify_backup(dump)["ok"] is True
    data = bytearray(dump.read_bytes())
    data[-10] ^= 0xFF
    dump.write_bytes(bytes(data))
    result = backup.verify_backup(dump)
    assert result["ok"] is False and "SHA-256" in result["problems"][0]


def _restore_db_exists(engine) -> bool:
    with engine.connect() as c:
        return bool(c.execute(text("SELECT 1 FROM pg_database WHERE datname = 'agentx_restore_check'")).scalar())


def test_restore_into_the_disposable_database_matches_the_backup(test_settings, client, engine, tmp_path):
    if not _restore_db_exists(engine):
        pytest.skip("agentx_restore_check is not provisioned (scripts/recovery/provision_restore_check.sql)")
    register_user(client)
    m = backup.create_backup(test_settings, tmp_path)
    report = backup.restore_check(test_settings, tmp_path / m["file"], "agentx_restore_check")
    assert report.ok, report.mismatches
    assert report.tables_compared == len(m["snapshot"]) and report.alembic_version == m["alembic_version"]


@pytest.mark.parametrize("confirm, refused", [(None, True), ("agentx", True), ("agentx_test", False)])
def test_staging_and_production_migrations_need_an_explicit_target(confirm, refused):
    env = {**os.environ, "ENVIRONMENT": "staging"}
    env.pop("AGENTX_MIGRATION_TARGET", None)
    if confirm:
        env["AGENTX_MIGRATION_TARGET"] = confirm
    r = subprocess.run([sys.executable, "-m", "alembic", "-x", "db=test", "current"], cwd=BACKEND_DIR, env=env,
                       capture_output=True, text=True)
    assert "alembic target database: agentx_test" in r.stderr
    assert (r.returncode != 0 and "refusing to migrate" in r.stderr) if refused else r.returncode == 0
