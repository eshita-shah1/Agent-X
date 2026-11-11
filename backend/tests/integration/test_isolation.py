import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.db.base import Base, UserOwnedMixin
from app.db.models import User, VirtualAccount
from app.db.scoping import owned_select
from tests.conftest import auth_headers, register_user


def test_each_user_sees_only_their_own_account(client, make_user):
    a, ha = make_user("111111.00")
    b, hb = make_user("222222.00")
    ra, rb = client.get("/api/v1/account", headers=ha), client.get("/api/v1/account", headers=hb)
    assert ra.json()["id"] == a["account"]["id"]
    assert rb.json()["id"] == b["account"]["id"]
    assert ra.json()["id"] != rb.json()["id"]
    assert float(ra.json()["initial_capital"]) == 111111.00
    assert float(rb.json()["initial_capital"]) == 222222.00


@pytest.mark.parametrize("how", ["query", "header", "path_style_query"])
def test_client_supplied_user_id_is_ignored(client, make_user, how):
    a, ha = make_user("111111.00")
    b, _ = make_user("222222.00")
    victim = b["user"]["id"]
    kwargs = {
        "query": {"params": {"user_id": victim}},
        "header": {"headers": {**ha, "X-User-Id": victim}},
        "path_style_query": {"params": {"userId": victim, "account_id": b["account"]["id"]}},
    }[how]
    kwargs.setdefault("headers", ha)
    resp = client.get("/api/v1/account", **kwargs)
    assert resp.status_code == 200
    assert resp.json()["id"] == a["account"]["id"]


def test_registration_ignores_a_client_supplied_user_id(client):
    forged = str(uuid.uuid4())
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "forge@example.com",
            "password": "correct-horse-battery-staple",
            "initial_capital": "1000",
            "id": forged,
            "user_id": forged,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["id"] != forged


def test_valid_signature_for_nonexistent_user_is_rejected(client, settings):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "iss": settings.jwt_issuer, "iat": now, "exp": now + timedelta(minutes=5)},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    assert client.get("/api/v1/account", headers=auth_headers(token)).status_code == 401


def test_owned_select_filters_by_user(client, db):
    a = register_user(client)
    b = register_user(client)
    rows = db.scalars(owned_select(VirtualAccount, uuid.UUID(a["user"]["id"]))).all()
    assert [str(r.id) for r in rows] == [a["account"]["id"]]
    assert b["account"]["id"] not in [str(r.id) for r in rows]


def test_owned_select_refuses_models_that_are_not_user_owned():
    with pytest.raises(TypeError):
        owned_select(User, uuid.uuid4())


def test_a_user_can_have_only_one_account(client, db):
    from sqlalchemy.exc import IntegrityError

    a = register_user(client)
    db.add(VirtualAccount(user_id=uuid.UUID(a["user"]["id"]), currency="INR", initial_capital=1))
    with pytest.raises(IntegrityError):
        db.flush()


# Every table must be classified on purpose. Adding a table in a later phase without adding it
# here fails this test, which forces an explicit decision about who owns the data.
GLOBAL_TABLES = {
    "data_providers", "instruments", "candles", "adjusted_candles", "corporate_actions",
    # Phase 2 market-data tables (shared by all users)
    "instrument_provider_symbols", "provider_capabilities", "provider_health_state", "data_ingestion_batches",
    "data_quality_issues", "data_restatement_events", "exchange_calendar_days",
}
# data_ingestion_jobs has a nullable requested_by_user_id; the API only ever shows a user their own jobs.
SYSTEM_TABLES = {"users", "audit_events", "data_ingestion_jobs"}
USER_OWNED_TABLES = {
    "virtual_accounts",
    # Phase 4: every risk, order and accounting record belongs to exactly one user.
    "risk_settings",
    "risk_state",
    "orders",
    "fills",
    "cash_ledger",
    "positions",
    "risk_decisions",
    "corporate_action_adjustments",  # migration 0007
    "dividend_credits",  # migration 0008
    "protective_exit_requirements",  # migration 0011
    # Phase 3 backtests (migration 0009): every result row carries its run's owner (an insert trigger requires it to
    # match the run's)
    "backtest_runs", "backtest_instrument_results", "backtest_selection_decisions", "backtest_trades", "backtest_events",
    "backtest_equity_points",
}


def test_every_table_is_classified():
    assert {t.name for t in Base.metadata.sorted_tables} == GLOBAL_TABLES | SYSTEM_TABLES | USER_OWNED_TABLES


def test_user_owned_tables_use_the_mixin_with_a_required_user_fk():
    for table in Base.metadata.sorted_tables:
        if table.name in USER_OWNED_TABLES:
            col = table.c.user_id
            assert not col.nullable
            assert {fk.column.table.name for fk in col.foreign_keys} == {"users"}
    mapped = {m.class_.__tablename__ for m in Base.registry.mappers if issubclass(m.class_, UserOwnedMixin)}
    assert mapped == USER_OWNED_TABLES


def test_global_tables_carry_no_user_id():
    for table in Base.metadata.sorted_tables:
        if table.name in GLOBAL_TABLES:
            assert "user_id" not in table.c
