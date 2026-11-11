"""Health, migrations and database-role checks."""

import time

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.db.models  # noqa: F401
from app.db.base import Base
from app.main import create_app
from tests.conftest import alembic_config


def test_liveness(client):
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_readiness_when_database_is_up(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 200 and resp.json() == {"status": "ready"}


def test_readiness_reports_503_when_database_is_unreachable(test_settings):
    broken = test_settings.model_copy(update={"db_port": 1, "db_connect_timeout_seconds": 2})
    with TestClient(create_app(broken)) as c:
        assert c.get("/health").status_code == 200  # liveness is independent of the database
        started = time.monotonic()
        resp = c.get("/health/ready")
        assert time.monotonic() - started < 30  # bounded by the connect timeout, not minutes
        assert resp.status_code == 503
        assert resp.json() == {"status": "unavailable"}
        assert "password" not in resp.text.lower()


def test_migrations_round_trip_and_match_the_models(engine):
    """downgrade -> upgrade works, and the migrated schema has no drift from the ORM models."""
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), "base")
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert tables <= {"alembic_version"}
        funcs = conn.execute(text("select count(*) from pg_proc where proname like '%reject%'")).scalar()
        assert funcs == 0  # trigger functions are dropped with the downgrade
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == []


def test_application_role_is_not_privileged(db):
    row = db.execute(
        text("select rolsuper, rolcreaterole, rolcreatedb, rolbypassrls from pg_roles where rolname = current_user")
    ).one()
    assert tuple(row) == (False, False, False, False)


def test_no_extensions_beyond_plpgsql_are_required(db):
    exts = {r[0] for r in db.execute(text("select extname from pg_extension"))}
    assert exts == {"plpgsql"}
