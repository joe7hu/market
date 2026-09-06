from __future__ import annotations

from logging.config import fileConfig

from alembic import context


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    raise RuntimeError("Use market-db-migrate so schema changes have a single-runner lock and time limits")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
