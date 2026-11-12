import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import app.db.models  # noqa: F401  (registers all models on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url():
    # `alembic -x db=test upgrade head` targets the test database; the default is the main one.
    settings = get_settings()
    target = context.get_x_argument(as_dictionary=True).get("db", "main")
    if target == "test":
        return settings.test_database_url
    if target == "main":
        return settings.database_url
    raise SystemExit(f"unknown -x db={target!r} (use 'main' or 'test')")


def _confirm_target(database: str) -> None:
    """Phase 8 wrong-database guard for command-line migrations (tests hand in their own connection and skip it).
    The target is always announced; in staging and production it must also be confirmed explicitly with
    AGENTX_MIGRATION_TARGET=<database>, so a migration can never run against an unintended database by accident."""
    settings = get_settings()
    print(f"alembic target database: {database} (ENVIRONMENT={settings.environment})", file=sys.stderr)
    if settings.environment in ("staging", "production") and os.environ.get("AGENTX_MIGRATION_TARGET") != database:
        raise SystemExit(
            f"refusing to migrate {database!r} in {settings.environment}: back it up first (python -m app.ops.cli backup), "
            f"then set AGENTX_MIGRATION_TARGET={database} to confirm"
        )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests may hand in an open connection via config.attributes["connection"].
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = _url()
    _confirm_target(url.database)
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
