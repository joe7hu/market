"""Explicit Alembic migration entrypoint for the PostgreSQL authority."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


HEAD_REVISION = "20260711_0003"


def alembic_config(dsn: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    return config


def upgrade_database(dsn: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(dsn), revision)


def downgrade_database(dsn: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(dsn), revision)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate the Market PostgreSQL database")
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    dsn = os.environ.get("MARKET_DATABASE_URL", "postgresql:///market")
    upgrade_database(dsn, args.revision)
