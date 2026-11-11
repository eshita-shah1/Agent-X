"""Shared fixtures. Integration tests run against a real PostgreSQL (the agentx_test database):
the immutability triggers, check constraints and timestamptz behaviour cannot be exercised on SQLite."""

import itertools
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.db.base import Base
from app.main import create_app

BACKEND_DIR = PROJECT_ROOT / "backend"
STRONG_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = get_settings()
    # Safety rail: tests TRUNCATE tables, so they must never be pointed at a non-test database.
    assert s.db_test_name.endswith("_test"), "DB_TEST_NAME must end in '_test'"
    assert s.db_test_name != s.db_name
    return s


@pytest.fixture(scope="session")
def test_settings(settings: Settings) -> Settings:
    """Settings whose database is the test database."""
    return settings.model_copy(update={"db_name": settings.db_test_name})


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    eng = create_engine(settings.test_database_url)
    yield eng
    eng.dispose()


def alembic_config(connection) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")


@pytest.fixture(autouse=True)
def clean_db(engine: Engine, migrated) -> None:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        # TRUNCATE does not fire the row-level immutability triggers; it is the test reset path only.
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings)) as c:
        yield c


_counter = itertools.count()


def register_user(client: TestClient, capital: str = "100000.00", email: str | None = None) -> dict:
    email = email or f"user{next(_counter)}-{uuid.uuid4().hex[:6]}@example.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": STRONG_PASSWORD, "initial_capital": capital},
    )
    assert resp.status_code == 201, resp.text
    return {"email": email, **resp.json()}


def login(client: TestClient, email: str, password: str = STRONG_PASSWORD) -> str:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def make_user(client: TestClient):
    """Registers a user and returns (user_info, auth headers)."""

    def _make(capital: str = "100000.00"):
        info = register_user(client, capital)
        return info, auth_headers(login(client, info["email"]))

    return _make
