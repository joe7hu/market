"""Explicit Alembic migration entrypoint for the PostgreSQL authority."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

HEAD_REVISION = "20260906_0001"


def alembic_config(dsn: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%"))
    return config


def upgrade_database(dsn: str, revision: str = "head") -> None:
    _migrate(dsn, revision, downgrade=False)


def downgrade_database(dsn: str, revision: str = "base") -> None:
    _migrate(dsn, revision, downgrade=True)


def _migrate(dsn: str, revision: str, *, downgrade: bool) -> None:
    # Runtime readers only need HEAD_REVISION; migration assets live in the checkout.
    from migrations.baseline_contract import (
        BASELINE_REVISION, BASELINE_SCHEMA_HASHES, LEGACY_REVISION, LEGACY_SCHEMA_HASHES,
    )
    from migrations.baseline_support import repair_legacy_schema, validate_roles
    from migrations.schema_contract import schema_hash

    config = alembic_config(dsn)
    engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            owner = connection.execute(text("SELECT current_user")).scalar()
            if owner in {"market_app", "market_research_signer", "market_migrator", os.environ.get("MARKET_APP_LOGIN_ROLE", "")}:
                raise RuntimeError("schema migration requires the separate migration owner connection")
            connection.commit()
            # A session lock survives future Alembic autocommit blocks.
            acquired = connection.execute(text(
                "SELECT pg_try_advisory_lock(hashtextextended('market-schema-migration',0))"
            )).scalar()
            connection.commit()
            if not acquired:
                raise RuntimeError("another Market schema migration is running")
            connection.exec_driver_sql("SET lock_timeout='2s'; SET statement_timeout='120s'")
            connection.commit()
            config.attributes["connection"] = connection
            if not downgrade:
                with connection.begin():
                    exists = connection.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
                    versions = connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all() if exists else []
                    if versions == [LEGACY_REVISION]:
                        validate_roles(connection)
                        if revision not in ("head", HEAD_REVISION):
                            raise RuntimeError("historical migrations are archived in Git; upgrade to the baseline")
                        if schema_hash(connection.connection.driver_connection) not in LEGACY_SCHEMA_HASHES:
                            raise RuntimeError("legacy schema differs from the verified baseline contract; adoption refused")
                        repair_legacy_schema(connection)
                        if schema_hash(connection.connection.driver_connection) not in BASELINE_SCHEMA_HASHES:
                            raise RuntimeError("repaired schema failed baseline verification; adoption rolled back")
                        connection.execute(text("UPDATE public.alembic_version SET version_num=:revision"), {"revision": BASELINE_REVISION})
            operation = command.downgrade if downgrade else command.upgrade
            operation(config, revision)
    finally:
        # NullPool closes the session, releasing the migration lock on all exits.
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate the Market PostgreSQL database")
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    dsn = os.environ.get("MARKET_DATABASE_URL", "postgresql:///market")
    upgrade_database(dsn, args.revision)
