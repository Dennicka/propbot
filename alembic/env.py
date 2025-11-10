from __future__ import annotations
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from alembic import context
import os
import pathlib
import sys

# this is the Alembic Config object, which provides access to the values within the .ini file.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = None

def get_url():
    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/crypto_bot.db")
    return db_url

def run_migrations_offline():
    url = get_url()
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = context.config.attributes.get("connection", None)
    if connectable is None:
        from sqlalchemy import create_engine
        connectable = create_engine(get_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
