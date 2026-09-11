"""Explicit Alembic migration entrypoint for the PostgreSQL authority."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

HEAD_REVISION = "20260911_0019"
_SUPPORTED_UPGRADE_REVISIONS = frozenset({
    "20260907_0006", "20260908_0007", "20260909_0008", "20260909_0009", "20260909_0010", "20260910_0011", "20260910_0012", "20260910_0013", "20260910_0014", "20260910_0015", "20260910_0016", "20260910_0017", "20260911_0018", HEAD_REVISION,
})
_MIGRATION_LOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('market-schema-migration',0))"


def alembic_config(dsn: str) -> Config:
    root = _migration_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%"))
    return config


def _migration_root() -> Path:
    configured_root = os.environ.get("MARKET_MIGRATIONS_ROOT", "").strip()
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        if (root / "alembic.ini").is_file() and (root / "migrations" / "env.py").is_file():
            return root
        raise RuntimeError(f"Market migration assets are incomplete at {root}")
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").is_file() and (parent / "migrations" / "env.py").is_file():
            return parent
    raise RuntimeError("Market migration assets are not installed; run from a source checkout with migrations/")


def upgrade_database(dsn: str, revision: str = "head") -> None:
    _migrate(dsn, revision, downgrade=False)


def downgrade_database(dsn: str, revision: str = "base") -> None:
    _migrate(dsn, revision, downgrade=True)


def _migrate(dsn: str, revision: str, *, downgrade: bool) -> None:
    # Runtime readers only need HEAD_REVISION; migration assets live in the checkout.
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
            try:
                connection.exec_driver_sql("SET lock_timeout='2s'; SET statement_timeout='120s'")
                connection.commit()
                config.attributes["connection"] = connection
                with connection.begin():
                    exists = connection.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
                    versions = connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all() if exists else []
                    if versions and (
                        len(versions) != 1 or versions[0] not in _SUPPORTED_UPGRADE_REVISIONS
                    ):
                        raise RuntimeError(
                            "database uses an archived migration revision; use the matching old checkout before switching to the current schema"
                        )
                operation = command.downgrade if downgrade else command.upgrade
                operation(config, revision)
            except BaseException:
                # Raw psycopg execution can outlive SQLAlchemy's transaction wrapper on errors.
                connection.rollback()
                raise
            finally:
                try:
                    connection.exec_driver_sql(_MIGRATION_LOCK_SQL)
                    connection.commit()
                except Exception:
                    connection.invalidate()
    finally:
        # NullPool closes the session; explicit unlock above covers failed raw DDL execution.
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate the Market PostgreSQL database")
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    dsn = os.environ.get("MARKET_DATABASE_URL", "postgresql:///market")
    upgrade_database(dsn, args.revision)
