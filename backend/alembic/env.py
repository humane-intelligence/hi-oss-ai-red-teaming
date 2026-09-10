"""Alembic async migration environment.

Three execution modes
---------------------
Online (normal CLI use):
    alembic reads sqlalchemy.url from this file (set from Settings) and creates
    an async engine via async_engine_from_config, then runs migrations over it.

Offline (SQL script generation, no live DB needed):
    Invoked via `make migrationsql`. Strips the async driver from the URL so
    Alembic can resolve the PostgreSQL dialect and emit raw SQL to stdout.
    No driver is loaded; no connection is made.

Test mode:
    The conftest passes a sync Connection via config.attributes["connection"].
    env.py detects this and calls do_run_migrations() directly, skipping the
    async engine setup entirely.  The connection is transaction-bound so all
    migration DDL is rolled back at the end of the test session.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

import app.models  # noqa: F401 — populates SQLModel.metadata with all table models
from alembic import context
from app.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = get_settings().database_url.replace("+asyncpg", "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # Test mode: conftest injects a pre-existing sync Connection so migrations
    # run inside the test transaction and are rolled back at session teardown.
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())
