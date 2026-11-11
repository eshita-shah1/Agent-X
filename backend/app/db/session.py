from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: URL, connect_timeout_seconds: int) -> Engine:
    # pool_pre_ping drops dead connections (e.g. after a PostgreSQL restart) instead of failing a request.
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": connect_timeout_seconds})


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One session per request; the caller commits explicitly, anything uncommitted is rolled back."""
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
