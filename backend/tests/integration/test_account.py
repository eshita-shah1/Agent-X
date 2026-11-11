"""Account baseline immutability and registration atomicity."""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models import VirtualAccount
from app.main import create_app
from tests.conftest import STRONG_PASSWORD, register_user

REGISTER_BODY = {"email": "atomic@example.com", "password": STRONG_PASSWORD, "initial_capital": "1000"}


def _count(db, table: str) -> int:
    return db.scalar(text(f"select count(*) from {table}"))


# ---- immutability ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "initial_capital = initial_capital + 1",
        "initial_capital = 1",
        "currency = 'USD'",
        "user_id = gen_random_uuid()",
        "id = gen_random_uuid()",
        "created_at = created_at + interval '1 day'",
    ],
)
def test_account_baseline_fields_cannot_be_updated(client, db, assignment):
    info = register_user(client, capital="12345.67")
    with pytest.raises((IntegrityError, DBAPIError)) as exc:
        db.execute(text(f"update virtual_accounts set {assignment}"))
        db.commit()
    db.rollback()
    assert "immutable" in str(exc.value)
    row = db.get(VirtualAccount, uuid.UUID(info["account"]["id"]))
    assert row.initial_capital == Decimal("12345.67")
    assert row.currency == "INR"


def test_account_cannot_be_updated_through_the_orm(client, db):
    info = register_user(client, capital="5000")
    account = db.get(VirtualAccount, uuid.UUID(info["account"]["id"]))
    account.initial_capital = Decimal("9999999")
    with pytest.raises((IntegrityError, DBAPIError)):
        db.commit()
    db.rollback()
    db.expire_all()
    assert db.get(VirtualAccount, account.id).initial_capital == Decimal("5000.00")


def test_original_capital_is_recoverable_after_other_activity(client, db):
    info = register_user(client, capital="777.50")
    # Anything else touching the user (e.g. login updating its row) leaves the baseline alone.
    client.post("/api/v1/auth/login", json={"email": info["email"], "password": STRONG_PASSWORD})
    db.expire_all()
    assert db.get(VirtualAccount, uuid.UUID(info["account"]["id"])).initial_capital == Decimal("777.50")


# ---- registration atomicity ----------------------------------------------------------------


@pytest.mark.parametrize("failing_step", ["VirtualAccount", "AuditEvent"])
def test_registration_failure_after_user_insert_leaves_nothing_behind(test_settings, db, monkeypatch, failing_step):
    """The user is flushed first; if a later step fails, user, account and audit rows must all roll back."""
    import app.api.v1.auth as auth_module

    def boom(*args, **kwargs):
        raise RuntimeError(f"injected failure in {failing_step}")

    monkeypatch.setattr(auth_module, failing_step, boom)
    with TestClient(create_app(test_settings), raise_server_exceptions=False) as c:
        resp = c.post("/api/v1/auth/register", json=REGISTER_BODY)
    assert resp.status_code == 500
    assert _count(db, "users") == 0
    assert _count(db, "virtual_accounts") == 0
    assert _count(db, "audit_events") == 0


def test_registration_failure_at_commit_leaves_nothing_behind(test_settings, db, monkeypatch):
    """Failure of the final commit itself (e.g. connection loss) also leaves no partial state."""
    from sqlalchemy.orm import Session

    real_commit = Session.commit

    def failing_commit(self):
        self.rollback()
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)
    with TestClient(create_app(test_settings), raise_server_exceptions=False) as c:
        resp = c.post("/api/v1/auth/register", json=REGISTER_BODY)
    monkeypatch.setattr(Session, "commit", real_commit)
    assert resp.status_code == 500
    assert (_count(db, "users"), _count(db, "virtual_accounts"), _count(db, "audit_events")) == (0, 0, 0)


def test_registration_succeeds_after_a_failed_attempt(test_settings, db, monkeypatch):
    import app.api.v1.auth as auth_module

    original = auth_module.VirtualAccount
    monkeypatch.setattr(auth_module, "VirtualAccount", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    with TestClient(create_app(test_settings), raise_server_exceptions=False) as c:
        assert c.post("/api/v1/auth/register", json=REGISTER_BODY).status_code == 500
        monkeypatch.setattr(auth_module, "VirtualAccount", original)
        # The email was not left reserved by the failed attempt.
        assert c.post("/api/v1/auth/register", json=REGISTER_BODY).status_code == 201
    assert (_count(db, "users"), _count(db, "virtual_accounts")) == (1, 1)
