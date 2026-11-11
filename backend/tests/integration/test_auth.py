import pytest
from sqlalchemy import select, text

from app.db.models import AuditEvent, User
from tests.conftest import STRONG_PASSWORD, auth_headers, login, register_user


def test_register_creates_user_and_account(client, db):
    info = register_user(client, capital="250000.50", email="  New.User@Example.COM ")
    assert info["user"]["email"] == "new.user@example.com"  # normalised
    assert info["account"]["currency"] == "INR"
    assert info["account"]["initial_capital"] in ("250000.50", 250000.5)
    assert "password" not in str(info).lower().replace("initial_capital", "")
    assert "password_hash" not in info["user"]


def test_password_is_stored_as_argon2id_never_plaintext(client, db):
    info = register_user(client)
    stored = db.scalar(select(User.password_hash).where(User.email == info["email"]))
    assert stored.startswith("$argon2id$")
    assert STRONG_PASSWORD not in stored


def test_duplicate_email_conflicts_case_insensitively(client):
    register_user(client, email="dup@example.com")
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "DUP@example.com", "password": STRONG_PASSWORD, "initial_capital": "1000"},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("capital", ["0", "-5", "abc", "10.123", None])
def test_invalid_initial_capital_rejected(client, capital):
    body = {"email": "cap@example.com", "password": STRONG_PASSWORD}
    if capital is not None:
        body["initial_capital"] = capital
    assert client.post("/api/v1/auth/register", json=body).status_code == 422


def test_initial_capital_has_no_default(client):
    resp = client.post("/api/v1/auth/register", json={"email": "n@example.com", "password": STRONG_PASSWORD})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": STRONG_PASSWORD, "initial_capital": "1000"},
        {"email": "a@example.com", "password": "short", "initial_capital": "1000"},
    ],
)
def test_invalid_registration_rejected(client, body):
    assert client.post("/api/v1/auth/register", json=body).status_code == 422


def test_failed_registration_creates_no_partial_rows(client, db):
    register_user(client, email="once@example.com")
    client.post(
        "/api/v1/auth/register",
        json={"email": "once@example.com", "password": STRONG_PASSWORD, "initial_capital": "1000"},
    )
    assert db.scalar(text("select count(*) from users")) == 1
    assert db.scalar(text("select count(*) from virtual_accounts")) == 1


def test_login_returns_working_token(client):
    info = register_user(client)
    token = login(client, info["email"])
    me = client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["id"] == info["user"]["id"]


def test_login_wrong_password_and_unknown_user_are_indistinguishable(client):
    info = register_user(client)
    bad_pw = client.post("/api/v1/auth/login", json={"email": info["email"], "password": "wrong-password-123"})
    unknown = client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-123"})
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()


def test_login_events_are_audited(client, db):
    info = register_user(client)
    client.post("/api/v1/auth/login", json={"email": info["email"], "password": "wrong-password-123"})
    login(client, info["email"])
    types = [e for (e,) in db.execute(select(AuditEvent.event_type).order_by(AuditEvent.id))]
    assert types == ["user.registered", "auth.login_failed", "auth.login_succeeded"]


def test_failed_login_audit_never_stores_the_password(client, db):
    info = register_user(client)
    client.post("/api/v1/auth/login", json={"email": info["email"], "password": "super-secret-attempt"})
    details = db.scalar(select(AuditEvent.details).where(AuditEvent.event_type == "auth.login_failed"))
    assert "super-secret-attempt" not in str(details)


def test_inactive_user_cannot_login_or_use_existing_token(client, db, engine):
    info = register_user(client)
    token = login(client, info["email"])
    with engine.begin() as conn:
        conn.execute(text("update users set is_active = false where email = :e"), {"e": info["email"]})
    assert client.get("/api/v1/auth/me", headers=auth_headers(token)).status_code == 401
    resp = client.post("/api/v1/auth/login", json={"email": info["email"], "password": STRONG_PASSWORD})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/auth/me", "/api/v1/account"])
def test_protected_endpoints_require_a_token(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer a.b.c", "Basic abc", ""])
def test_malformed_credentials_rejected(client, header):
    assert client.get("/api/v1/auth/me", headers={"Authorization": header}).status_code == 401
